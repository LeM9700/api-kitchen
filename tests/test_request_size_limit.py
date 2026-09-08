"""Limite de taille de requête HTTP appliquée au flux reçu (Prompt 10).

Constat corrigé :
- L'ancien ``_RequestSizeLimitMiddleware`` (voir git history sur
  ``app/main.py``) ne vérifiait QUE l'en-tête ``Content-Length``. Un client
  peut omettre cet en-tête (``Transfer-Encoding: chunked``) ou le falsifier
  (annoncer une petite valeur puis envoyer un corps plus volumineux) et
  passer au travers -- FastAPI lit alors le corps complet en mémoire avant
  toute validation Pydantic.

``app.core.http.request_size_limit.RequestSizeLimitMiddleware`` compte les
octets réellement reçus en enveloppant le callable ASGI ``receive`` : la
limite s'applique quel que soit l'en-tête, y compris absent ou mensonger.

Ces tests exercent le middleware à deux niveaux :
1. Niveau ASGI direct (rapide, déterministe, sans dépendance DB/Cloudinary) :
   Content-Length au-dessus de la limite, absence de Content-Length avec un
   corps volumineux, flux en plusieurs chunks (équivalent chunked), limites
   distinctes par catégorie de route (JSON / image / import CSV), upload
   autorisé sous la limite.
2. Niveau application réelle (``client`` fixture) : preuve que le middleware
   est bien monté sur l'app FastAPI en production.
"""

from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from app.core.http.request_size_limit import RequestSizeLimitMiddleware


async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """App ASGI factice en aval du middleware : consomme tout le corps reçu
    (comme le ferait Starlette/python-multipart) puis répond 200 avec le
    nombre d'octets lus -- prouve que le middleware laisse bien passer une
    requête sous la limite jusqu'à l'application.

    Sur ``http.disconnect`` (le middleware a déjà rejeté et envoyé son 413),
    ne répond rien -- comme le ferait une vraie app Starlette face à un
    client parti."""
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    response = PlainTextResponse(f"received={total}")
    await response(scope, receive, send)


def _make_scope(method: str, path: str, content_length: int | None = None) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }


def _make_chunked_receive(chunks: list[bytes]):
    """Simule un corps livré en plusieurs messages ``http.request`` -- le
    comportement ASGI réel pour ``Transfer-Encoding: chunked`` (le serveur
    ASGI, pas ce middleware, désencode le chunked-encoding HTTP/1.1 en une
    séquence de messages ; ce middleware ne voit jamais l'en-tête
    ``Transfer-Encoding`` lui-même, seulement cette séquence sans
    Content-Length -- exactement ce que cette fonction reproduit)."""
    remaining = list(chunks)

    async def _receive():
        chunk = remaining.pop(0) if remaining else b""
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(remaining),
        }

    return _receive


class _ResponseCollector:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


async def test_content_length_above_limit_is_rejected_without_reading_body():
    """Content-Length annoncé au-dessus de la limite -> 413 immédiat, sans
    même ouvrir le flux (rejet rapide, cas le plus simple)."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope("POST", "/api/v1/auth/login", content_length=5 * 1024 * 1024)
    send = _ResponseCollector()

    async def _receive():
        raise AssertionError("le corps ne doit jamais etre lu quand Content-Length rejette deja")

    await middleware(scope, _receive, send)

    assert send.status == 413
    assert b"PAYLOAD_TOO_LARGE" in send.body


async def test_missing_content_length_with_oversized_body_is_rejected():
    """Absence totale de Content-Length (cas qu'un client peut forger) avec
    un corps qui dépasse la limite au fil de l'eau -> 413 malgré tout."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope("POST", "/api/v1/auth/login", content_length=None)
    # 2 Mo au total, largement au-dessus de MAX_BYTES_JSON (1 Mo), livres en
    # un seul message -- Content-Length absent du scope.
    receive = _make_chunked_receive([b"x" * (2 * 1024 * 1024)])
    send = _ResponseCollector()

    await middleware(scope, receive, send)

    assert send.status == 413
    assert b"PAYLOAD_TOO_LARGE" in send.body


async def test_chunked_equivalent_multi_message_body_over_limit_is_rejected():
    """Flux livré en PLUSIEURS messages ``http.request`` (equivalent
    ``Transfer-Encoding: chunked``, jamais de Content-Length dans ce mode) --
    le middleware doit rejeter dès que le CUMUL dépasse la limite, pas
    seulement en lisant un seul gros message d'un coup."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope("POST", "/api/v1/auth/login", content_length=None)
    # 10 chunks de 200 Ko = 2 Mo au total, chaque chunk individuellement
    # petit -- seul le cumul depasse MAX_BYTES_JSON (1 Mo).
    chunk = b"y" * (200 * 1024)
    receive = _make_chunked_receive([chunk] * 10)
    send = _ResponseCollector()

    await middleware(scope, receive, send)

    assert send.status == 413
    assert b"PAYLOAD_TOO_LARGE" in send.body


async def test_default_json_route_rejects_body_over_1mb_even_under_image_limit():
    """Une route JSON standard (pas un upload d'image ni un import CSV) doit
    rester plafonnee a MAX_BYTES_JSON (1 Mo) -- un corps de 3 Mo, qui serait
    autorise sur la route d'upload d'image, doit etre rejete ici : les
    limites sont bien distinctes PAR ROUTE, pas un plafond global unique."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope("POST", "/api/v1/catalog/products", content_length=None)
    receive = _make_chunked_receive([b"z" * (3 * 1024 * 1024)])
    send = _ResponseCollector()

    await middleware(scope, receive, send)

    assert send.status == 413


async def test_image_upload_route_allows_body_above_json_limit_but_under_image_limit():
    """Upload autorisé : un corps de 3 Mo (au-dessus de MAX_BYTES_JSON mais
    sous MAX_BYTES_IMAGE) sur la route d'upload d'image doit atteindre
    l'application en aval, pas être rejeté à tort par la limite JSON par
    défaut."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope(
        "POST", "/api/v1/catalog/products/42/images", content_length=3 * 1024 * 1024
    )
    body = b"i" * (3 * 1024 * 1024)
    receive = _make_chunked_receive([body])
    send = _ResponseCollector()

    await middleware(scope, receive, send)

    assert send.status == 200
    assert send.body == f"received={len(body)}".encode()


async def test_image_upload_route_rejects_body_above_image_limit():
    """Upload refusé : un corps au-dessus de MAX_BYTES_IMAGE (10 Mo) sur la
    route d'upload d'image doit être rejeté, même si aucun Content-Length
    n'est fourni (streaming multipart)."""
    middleware = RequestSizeLimitMiddleware(_echo_app)
    scope = _make_scope("POST", "/api/v1/catalog/products/42/images", content_length=None)
    # 11 Mo en plusieurs chunks -- au-dessus de MAX_BYTES_IMAGE (10 Mo).
    chunk = b"i" * (1024 * 1024)
    receive = _make_chunked_receive([chunk] * 11)
    send = _ResponseCollector()

    await middleware(scope, receive, send)

    assert send.status == 413
    assert b"image" in send.body


async def test_csv_import_route_has_its_own_distinct_limit():
    """Import CSV catalogue : limite distincte (5 Mo), ni la limite JSON (1
    Mo, trop stricte pour un gros catalogue) ni la limite image (10 Mo, trop
    large pour du texte)."""
    middleware = RequestSizeLimitMiddleware(_echo_app)

    # 3 Mo : au-dessus de la limite JSON, sous la limite CSV -- doit passer.
    scope_ok = _make_scope(
        "POST", "/api/v1/catalog/imports/csv/dry-run", content_length=3 * 1024 * 1024
    )
    body_ok = b"c" * (3 * 1024 * 1024)
    send_ok = _ResponseCollector()
    await middleware(scope_ok, _make_chunked_receive([body_ok]), send_ok)
    assert send_ok.status == 200

    # 6 Mo : au-dessus de la limite CSV (5 Mo) -- doit etre rejete.
    scope_over = _make_scope(
        "POST", "/api/v1/catalog/imports/csv/dry-run", content_length=None
    )
    chunk = b"c" * (1024 * 1024)
    send_over = _ResponseCollector()
    await middleware(scope_over, _make_chunked_receive([chunk] * 6), send_over)
    assert send_over.status == 413
    assert b"csv_import" in send_over.body


async def test_non_http_scope_passes_through_untouched():
    """Un scope non-HTTP (ex: websocket, lifespan) ne doit jamais être
    intercepté par ce middleware -- il ne concerne que les requêtes HTTP."""
    called = {}

    async def _downstream(scope, receive, send):
        called["scope_type"] = scope["type"]

    middleware = RequestSizeLimitMiddleware(_downstream)
    await middleware({"type": "websocket"}, _make_chunked_receive([]), _ResponseCollector())

    assert called["scope_type"] == "websocket"


# ---------------------------------------------------------------------------
# Niveau application reelle -- preuve que le middleware est bien monte.
# ---------------------------------------------------------------------------


async def test_real_app_rejects_oversized_json_body_via_content_length(client):
    """Preuve d'intégration : l'app FastAPI réelle rejette bien un corps trop
    volumineux sur une route JSON standard (login), via le middleware monté
    dans ``app.main.create_app``."""
    oversized_password = "x" * (2 * 1024 * 1024)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": "test", "email": "a@b.com", "password": oversized_password},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_real_app_rejects_oversized_body_sent_without_content_length(client):
    """Preuve d'intégration : même sans Content-Length (corps envoyé comme
    flux/générateur asynchrone, donc en ``Transfer-Encoding: chunked`` côté
    httpx), l'app réelle rejette toujours un corps trop volumineux."""
    import json

    payload = json.dumps(
        {"tenant_slug": "test", "email": "a@b.com", "password": "x" * (2 * 1024 * 1024)}
    ).encode()

    async def _stream():
        step = 64 * 1024
        for i in range(0, len(payload), step):
            yield payload[i : i + step]

    resp = await client.post(
        "/api/v1/auth/login",
        content=_stream(),
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers.keys()}
    assert resp.status_code == 413
    assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"
