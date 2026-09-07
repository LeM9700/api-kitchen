"""Correctif WebSocket sur le commit efff556 (branche permissions-revocation-mfa).

Constat corrigé :
- ``_redis_subscriber`` n'était jamais démarré par le lifespan FastAPI (le
  pub/sub notif:*/session_revoked:* restait mort en pratique) ;
- le module importait ``aioredis``, absent des dépendances du projet
  (``redis>=5.0`` fournit ``redis.asyncio``) ;
- le filet heartbeat ne relisait que des flags Redis, donc une WebSocket
  restait ouverte si Redis était indisponible malgré un compte désactivé.

Ces tests prouvent :
1. le lifespan démarre puis annule proprement la tâche du subscriber ;
2. un message pub/sub ``session_revoked:*`` ferme réellement une socket
   locale (à travers le vrai code de dispatch de ``_redis_subscriber``) ;
3. un compte devenu inactif est fermé par le heartbeat même sans Redis
   (revalidation PostgreSQL autoritaire) ;
4. le module n'importe plus ``aioredis``.
"""

import ast
import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
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


async def test_ws_handler_closes_inactive_account_without_redis(monkeypatch):
    """Un compte devenu inactif doit fermer la WebSocket via le heartbeat
    PostgreSQL même quand Redis n'est pas disponible (redis=None)."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    inactive_state = SimpleNamespace(
        id=42, email="staff@acme.test", role="staff", permissions=[], is_active=False
    )
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=inactive_state)
    ):
        await ws_router._ws_handler(websocket, "acme", 42, "conn-1", redis=None, jti=None)

    assert websocket.closed == (4009, "session_revoked")
    assert websocket.sent == []


async def test_ws_handler_keeps_open_when_account_active_and_no_redis(monkeypatch):
    """Contrôle négatif : un compte actif ne doit pas être fermé par le
    heartbeat -- seul le ping doit partir."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(ws_router, "HEARTBEAT_TIMEOUT", 0.01)

    active_state = SimpleNamespace(
        id=42, email="staff@acme.test", role="staff", permissions=[], is_active=True
    )
    websocket = _HangingWebSocket()

    with patch.object(
        ws_router, "get_live_tenant_user_state", AsyncMock(return_value=active_state)
    ):
        # Le ping part puis le pong n'arrive jamais (_HangingWebSocket) -> la
        # boucle se termine par timeout zombie, PAS par une fermeture 4009.
        await ws_router._ws_handler(websocket, "acme", 42, "conn-1", redis=None, jti=None)

    assert websocket.sent, "un ping aurait dû être envoyé"
    assert websocket.closed is None
