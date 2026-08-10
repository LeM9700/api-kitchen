"""Tests pour app.modules.pos.service -- flux OAuth de connexion POS.

Suit le style de tests/test_token_revocation.py : AsyncMock pour Redis, pas
de connexion reelle. Les tests de persistance (get_public_session) et
d'echange HTTP arrivent dans les taches suivantes.
"""
import pytest
from unittest.mock import AsyncMock

from app.core.http.errors import AppError
from app.modules.pos import service


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.getdel = AsyncMock(return_value=None)
    return redis


def test_generate_state_is_url_safe_and_long_enough():
    state = service.generate_state()
    assert len(state) >= 32
    assert all(c.isalnum() or c in "-_" for c in state)


def test_generate_state_is_unique_across_calls():
    assert service.generate_state() != service.generate_state()


async def test_store_oauth_state_sets_key_with_ttl(mock_redis):
    await service.store_oauth_state(mock_redis, "abc123", "acme")
    mock_redis.setex.assert_called_once_with(
        "pos_oauth_state:abc123", service.STATE_TTL_SECONDS, "acme"
    )


async def test_consume_oauth_state_returns_tenant_slug(mock_redis):
    mock_redis.getdel.return_value = "acme"
    result = await service.consume_oauth_state(mock_redis, "abc123")
    assert result == "acme"
    mock_redis.getdel.assert_called_once_with("pos_oauth_state:abc123")


async def test_consume_oauth_state_decodes_bytes(mock_redis):
    mock_redis.getdel.return_value = b"acme"
    assert await service.consume_oauth_state(mock_redis, "abc123") == "acme"


async def test_consume_oauth_state_raises_when_missing_or_expired(mock_redis):
    mock_redis.getdel.return_value = None
    with pytest.raises(AppError) as exc_info:
        await service.consume_oauth_state(mock_redis, "unknown")
    assert exc_info.value.code == "POS_OAUTH_INVALID_STATE"
    assert exc_info.value.status_code == 400


class _StatefulFakeRedis:
    """Double minimal avec un vrai GETDEL (dict), pour prouver l'usage unique
    de bout en bout -- pas seulement que consume_oauth_state lit un mock."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self._store[key] = value

    async def getdel(self, key):
        return self._store.pop(key, None)


async def test_consume_oauth_state_second_consumption_is_rejected_replay():
    redis = _StatefulFakeRedis()
    await service.store_oauth_state(redis, "abc123", "acme")

    first = await service.consume_oauth_state(redis, "abc123")
    assert first == "acme"

    with pytest.raises(AppError) as exc_info:
        await service.consume_oauth_state(redis, "abc123")
    assert exc_info.value.code == "POS_OAUTH_INVALID_STATE"
