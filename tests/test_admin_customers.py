from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_bulk_customer_message_requires_explicit_confirmation():
    from pydantic import ValidationError

    from app.modules.admin.customers.schemas import BulkCustomerMessageRequest

    with pytest.raises(ValidationError):
        BulkCustomerMessageRequest(
            channels=["push"],
            message_type="marketing",
            body="Promo",
            customer_ids=[1, 2],
            confirm_bulk_send=False,
        )


def test_marketing_message_respects_opt_in_flags():
    from app.modules.admin.customers.service import _can_send
    from app.modules.auth.models import User

    user = User(
        id=1,
        email="customer@example.com",
        password_hash="x",
        role="customer",
        marketing_email_opt_in=False,
        marketing_push_opt_in=True,
    )

    assert _can_send(user, "email", "marketing") is False
    assert _can_send(user, "push", "marketing") is True
    assert _can_send(user, "email", "transactional") is True


async def test_cancel_stale_pending_orders_cancels_and_notifies(monkeypatch):
    from app.modules.orders.models import Order
    from worker.tasks import order_expiration

    order = Order(
        id=123,
        user_id=7,
        status="pending",
        payment_status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    order_result = MagicMock()
    order_result.scalars.return_value = [order]
    payment_result = MagicMock()
    payment_result.scalars.return_value = []

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[order_result, payment_result])
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    class _SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    redis = AsyncMock()

    monkeypatch.setattr(order_expiration, "_active_tenant_slugs", AsyncMock(return_value=["acme"]))
    monkeypatch.setattr(order_expiration, "get_tenant_session", lambda tenant_slug: _SessionContext())
    monkeypatch.setattr(order_expiration, "notify_user", AsyncMock())

    await order_expiration.cancel_stale_pending_orders({"redis": redis})

    assert order.status == "cancelled"
    session.commit.assert_awaited_once()
    redis.enqueue_job.assert_awaited_once()
    order_expiration.notify_user.assert_awaited_once()
