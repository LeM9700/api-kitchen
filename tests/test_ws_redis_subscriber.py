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
   contre une vraie session tenant PostgreSQL.
"""

import ast
import asyncio
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
