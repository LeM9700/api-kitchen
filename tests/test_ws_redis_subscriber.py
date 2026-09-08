"""Correctifs WebSocket sur les commits efff556/8b20128 (branche permissions-revocation-mfa).

Constat corrigé (8b20128) :
- ``_redis_subscriber`` n'était jamais démarré par le lifespan FastAPI (le
  pub/sub notif:*/session_revoked:* restait mort en pratique) ;
- le module importait ``aioredis``, absent des dépendances du projet
  (``redis>=5.0`` fournit ``redis.asyncio``) ;
- le filet heartbeat ne relisait que ``users.is_active``, donc un simple
  retrait de permissions (sans désactivation) ne fermait jamais une
  WebSocket déjà ouverte si le pub/sub était manqué ou Redis indisponible --
  et l'expiration d'un access token HTTP n'a par elle-même aucun effet sur
  une connexion WebSocket déjà établie.

Constat corrigé (ce commit) :
- le heartbeat compare désormais un snapshot ``WsAuthState`` (role,
  permissions triées, is_active) capturé à l'authentification à un nouveau
  snapshot relu en PostgreSQL à chaque cycle -- tout écart (permission
  ajoutée/retirée, rôle changé, compte désactivé ou supprimé) ferme la
  connexion (code 4009), même sans Redis.

Ces tests prouvent :
1. le lifespan démarre puis annule proprement la tâche du subscriber ;
2. un message pub/sub ``session_revoked:*`` ferme réellement une socket
   locale (à travers le vrai code de dispatch de ``_redis_subscriber``) ;
3. avec ``redis=None`` : un retrait de permission ferme la connexion ; un
   changement de rôle ferme la connexion ; un état identique la conserve ;
   un compte devenu inactif la ferme (test préexistant conservé) ;
4. le module n'importe plus ``aioredis`` ;
5. ``_fetch_ws_auth_state`` (utilisé à l'authentification de la connexion
   pour capturer le snapshot de référence) lit bien l'état réel en base,
   contre une vraie session tenant PostgreSQL ;
6. une notification (canal ``notif:*``, pas seulement ``session_revoked:*``)
   publiée sur Redis est bien dispatchée par le vrai ``_redis_subscriber``
   vers ``broadcast_to_user`` -- critère d'acceptation du prompt "Démarrage
   fiable du subscriber Redis WebSocket" ;
7. bout en bout, deux "instances API" reliées uniquement par un broker
   pub/sub en mémoire (chacune avec son propre client Redis et son propre
   état local de connexions) : l'instance A publie via
   ``notification_service.notify_user``, l'instance B la reçoit via son
   propre ``_redis_subscriber`` et la livre à sa WebSocket locale.
"""

import ast
import asyncio
import contextlib
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from app.modules.notifications import ws_router


def test_ws_router_does_not_import_aioredis():
    """Le module ne doit plus contenir d'instruction ``import aioredis`` (le
    paquet n'est pas une dépendance du projet -- seul ``redis>=5.0`` /
    ``redis.asyncio`` l'est). Les mentions du nom dans des docstrings
    explicatives ("PAS aioredis") sont légitimes et ignorées ici : seuls les
    noeuds d'import réels de l'AST sont inspectés."""
    tree = ast.parse(inspect.getsource(ws_router))
    import_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            import_names.add(node.module.split(".")[0])

    assert "aioredis" not in import_names
    assert not hasattr(ws_router, "aioredis")


async def test_lifespan_starts_and_cancels_ws_redis_subscriber():
    """app.main.lifespan démarre _redis_subscriber comme tâche unique sur
    app.state, et l'annule/attend proprement à la sortie du contexte."""
    import app.main as main_module

    fresh_app = FastAPI()

    async def _never_ending() -> None:
        await asyncio.Event().wait()

    fake_pool = SimpleNamespace(close=AsyncMock(), wait_closed=AsyncMock())

    with (
        patch.object(ws_router, "_redis_subscriber", _never_ending),
        patch.object(main_module, "create_pool", AsyncMock(return_value=fake_pool)),
    ):
        async with main_module.lifespan(fresh_app):
            task = fresh_app.state.ws_redis_subscriber_task
            assert isinstance(task, asyncio.Task)
            assert not task.done()

        # Le context manager du lifespan a annulé et attendu la tâche.
        assert task.done()
        assert task.cancelled()


async def test_redis_subscriber_dispatches_session_revoked_to_close_connections():
    """Un message pub/sub sur session_revoked:* ferme réellement la WS locale
    correspondante -- exercice du vrai chemin de dispatch de _redis_subscriber,
    pas seulement de _close_user_connections en isolation."""

    class _FakeWS:
        def __init__(self):
            self.closed: tuple[int, str] | None = None

        async def close(self, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    class _FakePubSub:
        async def psubscribe(self, *patterns) -> None:
            return None

        async def listen(self):
            yield {
                "type": "pmessage",
                "channel": "session_revoked:acme:42",
                "data": json.dumps({"reason": "permissions_changed"}),
            }
            # Simule l'annulation de la tâche après traitement du message,
            # pour sortir proprement de la boucle `while True` du subscriber.
            raise asyncio.CancelledError()

        async def close(self) -> None:
            return None

    class _FakeRedisClient:
        def pubsub(self):
            return _FakePubSub()

        async def close(self) -> None:
            return None

    target_ws = _FakeWS()
    ws_router._connections["acme:42"] = {target_ws}
    try:
        with patch("redis.asyncio.from_url", return_value=_FakeRedisClient()):
            await ws_router._redis_subscriber()
    finally:
        ws_router._connections.pop("acme:42", None)

    assert target_ws.closed == (4009, "permissions_changed")


class _HangingWebSocket:
    """WebSocket dont ``receive_json`` ne repond jamais -- force le chemin
    ``asyncio.TimeoutError`` de ``_ws_handler`` a chaque cycle."""

    def __init__(self):
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []

    async def receive_json(self) -> dict:
        await asyncio.Event().wait()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


def _live_state(role: str, permissions: list[str], is_active: bool) -> SimpleNamespace:
    """Fabrique un objet compatible avec ``LiveTenantUserState`` pour mocker
    ``get_live_tenant_user_state`` (seuls role/permissions/is_active sont lus
    par ``_fetch_ws_auth_state``)."""
    return SimpleNamespace(
        id=42, email="staff@acme.test", role=role, permissions=permissions, is_active=is_active
    )


# Snapshot de référence capturé "à l'authentification" pour tous les tests
# heartbeat ci-dessous : staff actif avec deux permissions.
_REFERENCE_AUTH_STATE = ws_router.WsAuthState(
    role="staff", permissions=("orders:read", "stock:read"), is_active=True
)


async def test_ws_handler_closes_inactive_account_without_redis(monkeypatch):
    """Un compte devenu inactif doit fermer la WebSocket via le heartbeat
    PostgreSQL même quand Redis n'est pas disponible (redis=None)."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    inactive_state = _live_state("staff", ["orders:read", "stock:read"], is_active=False)
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=inactive_state)
    ):
        await ws_router._ws_handler(
            websocket, "acme", 42, "conn-1", _REFERENCE_AUTH_STATE, redis=None, jti=None
        )

    assert websocket.closed == (4009, "account_disabled")
    assert websocket.sent == []


async def test_ws_handler_closes_on_permission_removed_without_redis(monkeypatch):
    """Un retrait de permission (compte toujours actif, même rôle) doit
    fermer la WebSocket via le heartbeat PostgreSQL même sans Redis --
    c'est le risque résiduel corrigé par ce commit : ni le pub/sub (manqué),
    ni l'expiration du JWT HTTP (sans effet sur une WS déjà ouverte) ne sont
    nécessaires pour que la fermeture ait lieu."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    # "stock:read" a été retiré depuis la capture du snapshot de référence.
    reduced_state = _live_state("staff", ["orders:read"], is_active=True)
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=reduced_state)
    ):
        await ws_router._ws_handler(
            websocket, "acme", 42, "conn-1", _REFERENCE_AUTH_STATE, redis=None, jti=None
        )

    assert websocket.closed == (4009, "permissions_changed")
    assert websocket.sent == []


async def test_ws_handler_closes_on_role_changed_without_redis(monkeypatch):
    """Un changement de rôle (staff -> admin, ou l'inverse) doit fermer la
    WebSocket via le heartbeat PostgreSQL même sans Redis."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    promoted_state = _live_state("admin", ["orders:read", "stock:read"], is_active=True)
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=promoted_state)
    ):
        await ws_router._ws_handler(
            websocket, "acme", 42, "conn-1", _REFERENCE_AUTH_STATE, redis=None, jti=None
        )

    assert websocket.closed == (4009, "role_changed")
    assert websocket.sent == []


async def test_ws_handler_keeps_open_when_state_identical_and_no_redis(monkeypatch):
    """Contrôle négatif : un état PostgreSQL rigoureusement identique au
    snapshot de référence (même rôle, mêmes permissions, actif) ne doit pas
    fermer la connexion -- seul le ping doit partir."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT", 0.01)

    # Ordre de stockage volontairement différent : la comparaison normalise
    # via un tuple trié (voir WsAuthState), donc ceci ne doit PAS compter
    # comme un changement de permissions.
    unchanged_state = _live_state("staff", ["stock:read", "orders:read"], is_active=True)
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=unchanged_state)
    ):
        # Le ping part puis le pong n'arrive jamais (_HangingWebSocket) -> la
        # boucle se termine par timeout zombie, PAS par une fermeture 4009.
        await ws_router._ws_handler(
            websocket, "acme", 42, "conn-1", _REFERENCE_AUTH_STATE, redis=None, jti=None
        )

    assert websocket.sent, "un ping aurait dû être envoyé"
    assert websocket.closed is None


async def test_fetch_ws_auth_state_reads_live_tenant_state(client, unique_slug):
    """``_fetch_ws_auth_state`` -- utilisé par ``notifications_ws`` pour
    capturer le snapshot de référence à l'authentification de la connexion
    (objectif 1) -- doit refléter l'état réel en base contre une vraie
    session tenant PostgreSQL, jamais un JWT."""
    from app.core.auth.security import decode_token

    tenant_slug = f"wsauth{unique_slug}"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_slug,
            "email": f"admin-{unique_slug}@test.com",
            "password": "Valid1!aa",
        },
    )
    assert resp.status_code == 201, resp.text
    admin_id = int(decode_token(resp.json()["access_token"])["sub"])

    state = await ws_router._fetch_ws_auth_state(tenant_slug, admin_id)

    assert state is not None
    assert state.role == "admin"
    assert state.is_active is True

    missing = await ws_router._fetch_ws_auth_state(tenant_slug, admin_id + 999)
    assert missing is None


class _RecordingWebSocket:
    """WebSocket factice qui journalise les JSON envoyés et l'éventuelle fermeture."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


async def test_redis_subscriber_dispatches_notification_to_local_websocket():
    """Un message pub/sub sur ``notif:*`` (pas ``session_revoked:*``) doit être
    dispatché par le vrai ``_redis_subscriber`` vers ``broadcast_to_user`` et
    livré, avec son contenu exact, à la WebSocket locale correspondante --
    c'est le chemin emprunté par ``notification_service.notify_user`` en
    production, distinct du chemin de fermeture déjà couvert ci-dessus."""

    class _FakePubSub:
        async def psubscribe(self, *patterns) -> None:
            return None

        async def listen(self):
            yield {
                "type": "pmessage",
                "channel": "notif:acme:42",
                "data": json.dumps(
                    {
                        "type": "notification",
                        "event": "order.confirmed",
                        "title": "Commande confirmee",
                        "body": "Votre commande #42 a ete confirmee.",
                        "data": {"order_id": 42},
                        "notification_id": "abc123",
                        "timestamp": "2026-06-20T12:00:00+00:00",
                    }
                ),
            }
            # Simule l'annulation de la tâche après traitement du message.
            raise asyncio.CancelledError()

        async def close(self) -> None:
            return None

    class _FakeRedisClient:
        def pubsub(self):
            return _FakePubSub()

        async def close(self) -> None:
            return None

    target_ws = _RecordingWebSocket()
    ws_router._connections["acme:42"] = {target_ws}
    try:
        with patch("redis.asyncio.from_url", return_value=_FakeRedisClient()):
            await ws_router._redis_subscriber()
    finally:
        ws_router._connections.pop("acme:42", None)

    assert len(target_ws.sent) == 1
    assert target_ws.sent[0]["event"] == "order.confirmed"
    assert target_ws.sent[0]["notification_id"] == "abc123"
    assert target_ws.closed is None


class _InMemoryPubSubBroker:
    """Broker pub/sub minimal en mémoire, simulant Redis partagé entre
    plusieurs "instances API" dans un test contrôlé : chaque instance obtient
    son propre client (``new_client``), mais toutes les publications sont
    diffusées à tous les abonnés, exactement comme un vrai Redis le ferait
    entre deux process distincts."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []

    def new_client(self) -> "_BrokerRedisClient":
        return _BrokerRedisClient(self)

    def _register(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.append(queue)
        return queue

    async def _publish(self, channel: str, data: str) -> None:
        for queue in self._queues:
            await queue.put((channel, data))


class _BrokerPubSub:
    def __init__(self, broker: _InMemoryPubSubBroker):
        self._queue = broker._register()

    async def psubscribe(self, *patterns) -> None:
        return None

    async def listen(self):
        while True:
            channel, data = await self._queue.get()
            yield {"type": "pmessage", "channel": channel, "data": data}

    async def close(self) -> None:
        return None


class _BrokerRedisClient:
    """Client Redis factice branché sur le broker -- fournit ``publish`` (cote
    "instance A", utilise par ``notify_user``) et ``pubsub`` (cote "instance
    B", utilise par ``_redis_subscriber``)."""

    def __init__(self, broker: _InMemoryPubSubBroker):
        self._broker = broker

    def pubsub(self) -> _BrokerPubSub:
        return _BrokerPubSub(self._broker)

    async def publish(self, channel: str, data: str) -> None:
        await self._broker._publish(channel, data)

    async def close(self) -> None:
        return None


async def test_notification_published_by_one_instance_delivered_on_another_instance():
    """Critère d'acceptation du prompt : avec deux instances API, une
    notification publiée dans Redis par l'une est livrée à une WebSocket
    connectée sur l'autre.

    Simulation : deux clients Redis distincts (``instance_a_redis``,
    ``instance_b_redis``) branchés sur le même broker en mémoire -- aucun
    état partagé entre les deux sauf le pub/sub, comme deux process Railway
    distincts parlant au même Redis. L'instance A appelle le vrai
    ``notification_service.notify_user`` avec son client Redis (persistance
    push mockée -- hors périmètre de ce test) ; l'instance B fait tourner le
    vrai ``_redis_subscriber`` avec son propre client Redis et sa propre
    WebSocket locale (``ws_router._connections``, jamais peuplé côté A)."""
    from app.modules.notifications.notification_service import notify_user

    broker = _InMemoryPubSubBroker()
    instance_a_redis = broker.new_client()
    instance_b_redis = broker.new_client()

    target_ws = _RecordingWebSocket()
    ws_router._connections["acme:99"] = {target_ws}

    subscriber_task: asyncio.Task | None = None
    try:
        with patch("redis.asyncio.from_url", return_value=instance_b_redis):
            subscriber_task = asyncio.create_task(ws_router._redis_subscriber())

            # Laisse la coroutine "instance B" s'abonner avant de publier
            # depuis "instance A" (sinon le message serait perdu, comme avec
            # un vrai pub/sub Redis sans backlog).
            for _ in range(100):
                if broker._queues:
                    break
                await asyncio.sleep(0)
            assert broker._queues, "instance B ne s'est jamais abonnée"

            fake_session = AsyncMock()
            with patch(
                "app.modules.notifications.notification_service.send_push_notification",
                AsyncMock(return_value={"sent": 0, "failed": 0}),
            ):
                await notify_user(
                    session=fake_session,
                    tenant_slug="acme",
                    user_id=99,
                    event="order.confirmed",
                    title="Commande confirmee",
                    body="Votre commande #7 a ete confirmee.",
                    data={"order_id": 7},
                    redis=instance_a_redis,
                )

            for _ in range(100):
                if target_ws.sent:
                    break
                await asyncio.sleep(0)
    finally:
        ws_router._connections.pop("acme:99", None)
        if subscriber_task is not None:
            subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscriber_task

    assert len(target_ws.sent) == 1
    assert target_ws.sent[0]["event"] == "order.confirmed"
    assert target_ws.sent[0]["data"]["order_id"] == 7
    assert target_ws.closed is None


async def test_redis_subscriber_reconnects_after_connection_error(monkeypatch):
    """Reconnexion contrôlée : si la connexion Redis échoue (ou est perdue en
    plein listen), ``_redis_subscriber`` ne doit ni lever ni s'arrêter -- il
    retente après une pause courte, jusqu'à ce que Redis redevienne
    joignable, sans jamais perdre la boucle ``while True`` de fond."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    attempts = {"n": 0}

    class _FailingThenWorkingPubSub:
        async def psubscribe(self, *patterns) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("redis unreachable")
            return None

        async def listen(self):
            raise asyncio.CancelledError()
            yield  # pragma: no cover - rend la fonction generator

        async def close(self) -> None:
            return None

    class _FailingThenWorkingRedisClient:
        def pubsub(self):
            return _FailingThenWorkingPubSub()

        async def close(self) -> None:
            return None

    with patch("redis.asyncio.from_url", return_value=_FailingThenWorkingRedisClient()):
        await ws_router._redis_subscriber()

    # Une premiere tentative a echoue (ConnectionError), une seconde a
    # reussi puis s'est arretee proprement sur CancelledError -- la boucle a
    # bien retente au lieu de laisser l'exception se propager.
    assert attempts["n"] == 2
