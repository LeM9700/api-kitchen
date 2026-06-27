"""Tests for the MongoDB login event audit helper.

Uses AsyncMock — no real MongoDB connection required.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_login_audit_helper_writes_correct_document():
    from app.modules.auth.login_audit import log_login_event

    mock_db = MagicMock()
    mock_collection = AsyncMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)
    mock_collection.insert_one = AsyncMock()

    await log_login_event(
        db=mock_db,
        tenant_slug="test-tenant",
        email="user@test.com",
        user_id=42,
        success=True,
        failure_reason=None,
        ip_address="1.2.3.4",
        user_agent="TestBrowser/1.0",
    )

    mock_collection.insert_one.assert_called_once()
    doc = mock_collection.insert_one.call_args[0][0]
    assert doc["tenant_slug"] == "test-tenant"
    assert doc["email"] == "user@test.com"
    assert doc["user_id"] == 42
    assert doc["success"] is True
    assert doc["failure_reason"] is None
    assert "created_at" in doc


@pytest.mark.asyncio
async def test_login_audit_helper_writes_failure_document():
    from app.modules.auth.login_audit import log_login_event

    mock_db = MagicMock()
    mock_collection = AsyncMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)
    mock_collection.insert_one = AsyncMock()

    await log_login_event(
        db=mock_db,
        tenant_slug="test-tenant",
        email="user@test.com",
        user_id=None,
        success=False,
        failure_reason="INVALID_CREDENTIALS",
        ip_address="10.0.0.1",
        user_agent="curl/7.0",
    )

    mock_collection.insert_one.assert_called_once()
    doc = mock_collection.insert_one.call_args[0][0]
    assert doc["success"] is False
    assert doc["failure_reason"] == "INVALID_CREDENTIALS"
    assert doc["user_id"] is None
    assert doc["ip_address"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_login_audit_uses_correct_collection_name():
    from app.modules.auth.login_audit import log_login_event

    mock_db = MagicMock()
    mock_collection = AsyncMock()
    mock_collection.insert_one = AsyncMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)

    await log_login_event(
        db=mock_db,
        tenant_slug="my-pizzeria",
        email="a@b.com",
        user_id=1,
        success=True,
        failure_reason=None,
        ip_address=None,
        user_agent=None,
    )

    mock_db.__getitem__.assert_called_once_with("login_events_my-pizzeria")


@pytest.mark.asyncio
async def test_login_audit_swallows_exceptions():
    """Audit errors must never propagate — log_login_event must be fire-and-forget safe."""
    from app.modules.auth.login_audit import log_login_event

    mock_db = MagicMock()
    mock_collection = AsyncMock()
    mock_collection.insert_one = AsyncMock(side_effect=Exception("MongoDB connection lost"))
    mock_db.__getitem__ = MagicMock(return_value=mock_collection)

    # Must not raise
    await log_login_event(
        db=mock_db,
        tenant_slug="test-tenant",
        email="u@t.com",
        user_id=None,
        success=False,
        failure_reason="NETWORK_ERROR",
        ip_address=None,
        user_agent=None,
    )
