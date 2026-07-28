from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.http.errors import AppError
from app.modules.orders.models import Order
from app.modules.payments import service
from app.modules.payments.models import Payment
from app.modules.payments.schemas import (
    PaymentListItemOut,
    PaymentSummaryOut,
    RefundCreate,
)


def test_refund_create_requires_positive_amount():
    try:
        RefundCreate(amount=0)
    except Exception as exc:
        assert "greater than 0" in str(exc)
    else:
        raise AssertionError("RefundCreate(amount=0) should fail validation")


def test_payment_list_item_contains_interface_fields():
    item = PaymentListItemOut(
        id=1,
        order_id=2,
        provider="stripe",
        provider_payment_id="pi_123",
        provider_account_id="acct_123",
        amount=19.5,
        currency="EUR",
        status="paid",
        created_at=datetime.now(timezone.utc),
        refunded_amount_cents=500,
    )

    assert item.provider_account_id == "acct_123"
    assert item.refunded_amount_cents == 500


def test_payment_summary_exposes_financial_totals():
    summary = PaymentSummaryOut(
        collected_amount_cents=2000,
        refunded_amount_cents=500,
        net_amount_cents=1500,
        payment_count=2,
        refund_count=1,
        counts_by_status={"paid": 1, "refunded": 1},
    )

    assert summary.net_amount_cents == 1500
    assert summary.counts_by_status["paid"] == 1


async def test_create_manual_payment_refund_does_not_call_stripe():
    order = Order(id=1, status="cancelled", payment_status="paid", total=20)
    payment = Payment(id=2, order_id=1, provider="cash", amount=20, currency="EUR", status="paid")
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.scalar = AsyncMock(side_effect=[payment, 0])
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 3

    session.refresh = AsyncMock(side_effect=fake_refresh)

    with patch("app.modules.payments.service.stripe.Refund.create") as stripe_refund:
        refund = await service.create_refund(
            session,
            tenant_slug="acme",
            order_id=1,
            user_id=9,
            amount=None,
            reason="Retour especes",
        )

    stripe_refund.assert_not_called()
    assert refund.status == "succeeded"
    assert refund.reason == "Retour especes"
    assert payment.status == "refunded"


async def test_create_refund_requires_reason():
    session = AsyncMock()

    with pytest.raises(AppError) as exc_info:
        await service.create_refund(
            session,
            tenant_slug="acme",
            order_id=1,
            user_id=9,
            amount=None,
            reason="",
        )

    assert exc_info.value.code == "REFUND_REASON_REQUIRED"
