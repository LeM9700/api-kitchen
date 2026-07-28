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
