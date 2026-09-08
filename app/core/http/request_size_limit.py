"""Middleware ASGI de limite de taille de requête HTTP — appliquée au FLUX reçu.

[🔒 SÉCURITÉ] FastAPI/Starlette lit le body complet avant la validation
Pydantic — sans protection explicite, un attaquant peut envoyer des payloads
massifs (plusieurs centaines de Mo) qui saturent la RAM du processus uvicorn.

Un contrôle basé uniquement sur l'en-tête ``Content-Length`` (l'ancienne
version de ce middleware) est insuffisant : un client peut omettre cet en-tête
(``Transfer-Encoding: chunked``) ou le falsifier (annoncer une petite valeur
puis envoyer un corps plus volumineux — la plupart des serveurs HTTP ne
vérifient pas la cohérence). Ce middleware compte donc les octets réellement
reçus au fil de l'eau, en enveloppant le callable ASGI ``receive`` : la limite
s'applique quel que soit l'en-tête envoyé, y compris en son absence totale ou
en cas de ``Transfer-Encoding: chunked``.

``Content-Length`` reste utilisé comme rejet rapide (évite d'ouvrir le flux
pour un cas déjà tranché), mais jamais comme seule protection.
"""

import logging
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestSizeLimitMiddleware:
    """Limite la taille du corps HTTP réellement reçu, par catégorie de route.

    Limites distinctes et documentées (toutes en octets) :
    - ``MAX_BYTES_JSON`` (1 Mo) : défaut, toutes les routes JSON standard.
    - ``MAX_BYTES_IMAGE`` (10 Mo) : upload d'image catalogue uniquement
      (``POST /api/v1/catalog/{products,categories,extras,variants}/{id}/images``).
      Cloudinary revalide ensuite à 8 Mo côté application
      (``app.core.services.cloudinary.MAX_SIZE_BYTES``) -- cette limite HTTP
      reste volontairement un peu plus large pour laisser ce message d'erreur
      applicatif (plus précis : format, dimensions) se déclencher plutôt
      qu'un rejet générique 413 au niveau transport.
    - ``MAX_BYTES_CSV_IMPORT`` (5 Mo) : import CSV catalogue
      (``POST /api/v1/catalog/imports/csv/...``, corps JSON contenant
      ``csv_text`` -- peut être volumineux pour un gros catalogue). Les
      exports (``GET .../exports/csv``, ``GET /haccp/export/...``) n'ont pas
      de corps de requête à limiter ici.

    [⚠️ PROD] Comportement dégradé : aucun -- contrairement au rate limiter
    (voir ``app.core.http.limiter``), il n'y a pas de dépendance externe
    (Redis) dont l'indisponibilité pourrait dégrader ce contrôle. Il est
    purement local au process et fonctionne identiquement à chaque requête.
    """

    MAX_BYTES_JSON: int = 1 * 1024 * 1024
    MAX_BYTES_IMAGE: int = 10 * 1024 * 1024
    MAX_BYTES_CSV_IMPORT: int = 5 * 1024 * 1024

    _IMAGE_UPLOAD_RE = re.compile(
        r"^/api/v1/catalog/(products|categories|extras|variants)/\d+/images$"
    )
    _CSV_IMPORT_RE = re.compile(r"^/api/v1/catalog/imports/csv(/.*)?$")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _limit_for(self, method: str, path: str) -> tuple[int, str]:
        """Détermine la limite (octets) et son label applicables à cette requête.

        Args:
            method: Méthode HTTP (``scope["method"]``).
            path: Chemin de la requête (``scope["path"]``).

        Returns:
            Tuple ``(limite_en_octets, label)``.
        """
        if method == "POST" and self._IMAGE_UPLOAD_RE.match(path):
            return self.MAX_BYTES_IMAGE, "image"
        if method == "POST" and self._CSV_IMPORT_RE.match(path):
            return self.MAX_BYTES_CSV_IMPORT, "csv_import"
        return self.MAX_BYTES_JSON, "json"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET")
        path: str = scope.get("path", "")
        limit, label = self._limit_for(method, path)

        # Rejet rapide si Content-Length est present et deja au-dessus de la
        # limite -- evite d'ouvrir/consommer le flux pour un cas deja tranche.
        # Jamais utilise comme SEULE protection (voir docstring de module) :
        # absent ou mensonger, c'est l'enforcement au fil de l'eau ci-dessous
        # (_wrapped_receive) qui protege reellement.
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await self._reject(scope, receive, send, limit, label)
                    return
            except (ValueError, TypeError):
                pass

        received = 0
        rejected = False

        async def _wrapped_receive() -> Message:
            """Compte les octets reçus au fil de l'eau et coupe court dès que
            la limite est dépassée.

            [🔒 SÉCURITÉ] Envoie la réponse 413 directement ICI (pas via une
            exception propagée jusqu'à ``__call__``) puis simule un
            ``http.disconnect`` pour l'application en aval : lever une
            exception depuis ce ``receive`` traverse ``TenantMiddleware`` et
            ``SecurityHeadersMiddleware`` (tous deux ``BaseHTTPMiddleware`` --
            chacun relaie le corps via son propre ``receive`` enveloppé et un
            groupe de tâches anyio) et n'y survit pas fiablement : elle peut
            y être capturée/retransformée avant de remonter jusqu'à notre
            propre ``except`` ci-dessous, laissant passer une erreur générique
            au lieu de notre 413. Un simple ``http.disconnect`` relayé (une
            valeur de retour, pas une exception) traverse ces mêmes couches
            sans transformation.

            Returns:
                Le message ASGI reçu tel quel, ou ``{"type": "http.disconnect"}``
                une fois la limite dépassée (y compris pour tout appel
                ultérieur, la lecture du corps en amont ne reprend jamais).
            """
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    rejected = True
                    await self._reject(scope, receive, send, limit, label)
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, _wrapped_receive, send)
        except Exception:
            if not rejected:
                raise
            # [⚠️ PROD] Le 413 a deja ete envoye au client juste au-dessus.
            # L'app en aval, en reagissant au http.disconnect simule (a
            # travers TenantMiddleware/SecurityHeadersMiddleware), peut lever
            # une erreur secondaire sans consequence pour le client deja
            # servi (ex: RuntimeError("No response returned.") de
            # starlette.middleware.base quand aucune reponse n'a ete
            # capturee par une des couches BaseHTTPMiddleware imbriquees) --
            # sans interet a logger en erreur ni a laisser remonter.
            logger.debug(
                "request_size_limit: erreur secondaire ignoree apres rejet 413 "
                "(reponse deja envoyee) label=%s limit=%s", label, limit,
            )

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, limit: int, label: str
    ) -> None:
        """Envoie la réponse 413 -- sûr à appeler même après lecture partielle
        du corps : l'application en aval n'a pas encore pu émettre de réponse
        tant qu'elle attendait la suite du corps (voir docstring de module)."""
        response = JSONResponse(
            {
                "code": "PAYLOAD_TOO_LARGE",
                "detail": f"Body trop volumineux (max {limit // (1024 * 1024)} Mo pour {label}).",
            },
            status_code=413,
        )
        await response(scope, receive, send)
