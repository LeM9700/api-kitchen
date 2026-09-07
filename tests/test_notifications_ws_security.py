import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.modules.notifications import ws_router


class _FakeRedis:
    def __init__(self, revoked_jti: str | None = None):
        self.revoked_jti = revoked_jti
        self.sadd_called = False

    async def exists(self, key: str) -> int:
        if key == f"jti:{self.revoked_jti}":
            return 1
        return 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def ttl(self, key: str) -> int:
        return 0

    async def scard(self, key: str) -> int:
        return 0

    async def sadd(self, key: str, value: str) -> None:
        self.sadd_called = True


class _FakeWebSocket:
    def __init__(self, redis: _FakeRedis, auth_message: dict):
        self.app = SimpleNamespace(state=SimpleNamespace(arq_pool=redis))
        self.client = SimpleNamespace(host="127.0.0.1")
        self.auth_message = auth_message
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        return self.auth_message

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_notifications_ws_rejects_revoked_access_token():
    redis = _FakeRedis(revoked_jti="revoked-jti")
    websocket = _FakeWebSocket(redis, {"type": "auth", "token": "token"})

    with patch.object(
        ws_router,
        "decode_token",
        return_value={
            "type": "access",
            "sub": "42",
            "tenant_slug": "acme",
            "role": "customer",
            "jti": "revoked-jti",
        },
    ):
        await ws_router.notifications_ws(websocket, tenant_slug="acme")

    assert websocket.accepted is True
    assert websocket.sent[0] == {"type": "auth_required"}
    assert websocket.sent[-1]["code"] == "unauthorized"
    assert websocket.closed == (4001, "Unauthorized")
    assert redis.sadd_called is False


@pytest.mark.asyncio
async def test_notifications_ws_rejects_user_not_belonging_to_tenant():
    """Meme gap que tests/test_jwt_tenant_mismatch.py, cote WebSocket : un
    token signe pour un ``sub`` d'un tenant mais reclamant un autre
    ``tenant_slug`` (identique a celui passe en query param, donc le check
    de correspondance nominal passe) ne doit pas etre accepte."""
    redis = _FakeRedis()
    websocket = _FakeWebSocket(redis, {"type": "auth", "token": "token"})

    with (
        patch.object(
            ws_router,
            "decode_token",
            return_value={
                "type": "access",
                "sub": "1",
                "tenant_slug": "acme",
                "role": "admin",
                "jti": "not-revoked",
            },
        ),
        patch.object(ws_router, "user_belongs_to_tenant", return_value=False) as mocked,
    ):
        await ws_router.notifications_ws(websocket, tenant_slug="acme")

    mocked.assert_awaited_once_with(1, "acme", None)
    assert websocket.accepted is True
    assert websocket.sent[-1]["code"] == "unauthorized"
    assert websocket.closed == (4001, "Unauthorized")
    assert redis.sadd_called is False


class _HangingWebSocket:
    """WebSocket dont ``receive_json`` ne repond jamais -- force le chemin
    ``asyncio.TimeoutError`` de ``_ws_handler`` a chaque cycle, quel que soit
    ``HEARTBEAT_INTERVAL`` (monkeypatche a une valeur minuscule dans les tests
    pour ne pas attendre 30s reelles)."""

    def __init__(self):
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []

    async def receive_json(self) -> dict:
        await asyncio.Event().wait()  # ne se resout jamais

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_ws_handler_closes_when_user_disabled_between_heartbeats(monkeypatch):
    """Un compte desactive APRES l'ouverture de la WS doit voir sa connexion
    fermee au heartbeat suivant, sans attendre l'expiration du token."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    class _Redis:
        async def exists(self, key: str) -> int:
            return 1 if key == "user_disabled:acme:42" else 0

    websocket = _HangingWebSocket()

    await ws_router._ws_handler(
        websocket, "acme", 42, "conn-1", redis=_Redis(), jti="some-jti"
    )

    assert websocket.closed == (4009, "session_revoked")
    assert websocket.sent == []  # ferme avant meme d'envoyer un ping


@pytest.mark.asyncio
async def test_ws_handler_closes_when_jti_revoked_between_heartbeats(monkeypatch):
    """Un logout qui revoque le jti de l'access token ayant authentifie la WS
    doit fermer cette WS au heartbeat suivant."""
    monkeypatch.setattr(ws_router, "HEARTBEAT_INTERVAL", 0.01)

    class _Redis:
        async def exists(self, key: str) -> int:
            return 1 if key == "jti:revoked-jti" else 0

    websocket = _HangingWebSocket()

    await ws_router._ws_handler(
        websocket, "acme", 42, "conn-1", redis=_Redis(), jti="revoked-jti"
    )

    assert websocket.closed == (4009, "session_revoked")


@pytest.mark.asyncio
async def test_close_user_connections_closes_matching_local_sockets():
    """``_close_user_connections`` ferme toutes les WS locales de la cle
    tenant:user et n'affecte pas les connexions d'un autre utilisateur."""

    class _FakeWS:
        def __init__(self):
            self.closed: tuple[int, str] | None = None

        async def close(self, code: int, reason: str = "") -> None:
            self.closed = (code, reason)

    target_ws = _FakeWS()
    other_ws = _FakeWS()
    ws_router._connections["acme:42"] = {target_ws}
    ws_router._connections["acme:99"] = {other_ws}
    try:
        await ws_router._close_user_connections("acme", 42, code=4009, reason="permissions_changed")
    finally:
        ws_router._connections.pop("acme:42", None)
        ws_router._connections.pop("acme:99", None)

    assert target_ws.closed == (4009, "permissions_changed")
    assert other_ws.closed is None
