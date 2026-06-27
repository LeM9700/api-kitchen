from datetime import datetime, timezone

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
