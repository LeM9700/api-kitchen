"""Tests for JTI deny-list Redis infrastructure.

Uses AsyncMock — no real Redis connection required.
All test functions are async and discovered by pytest-asyncio.
"""
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone, timedelta

from app.core.auth.token_revocation import (
    revoke_jti,
    is_jti_revoked,
    flag_user_disabled,
    is_user_disabled,
    clear_user_disabled,
)


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_revoke_jti_sets_key_with_ttl(mock_redis):
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    await revoke_jti(mock_redis, "test-jti-123", expires_at)
    mock_redis.setex.assert_called_once()
    args = mock_redis.setex.call_args[0]
    assert args[0] == "jti:test-jti-123"
    assert isinstance(args[1], int) and args[1] > 0
    assert args[2] == "1"


@pytest.mark.asyncio
async def test_is_jti_revoked_returns_false_when_not_in_redis(mock_redis):
    mock_redis.exists.return_value = 0
    assert await is_jti_revoked(mock_redis, "unknown-jti") is False


@pytest.mark.asyncio
async def test_is_jti_revoked_returns_true_when_in_redis(mock_redis):
    mock_redis.exists.return_value = 1
    assert await is_jti_revoked(mock_redis, "known-jti") is True


@pytest.mark.asyncio
async def test_flag_user_disabled_sets_key(mock_redis):
    await flag_user_disabled(mock_redis, 42)
    mock_redis.set.assert_called_once_with("user_disabled:42", "1", ex=86400)


@pytest.mark.asyncio
async def test_is_user_disabled(mock_redis):
    mock_redis.exists.return_value = 1
    assert await is_user_disabled(mock_redis, 42) is True


@pytest.mark.asyncio
async def test_clear_user_disabled(mock_redis):
    await clear_user_disabled(mock_redis, 42)
    mock_redis.delete.assert_called_once_with("user_disabled:42")
