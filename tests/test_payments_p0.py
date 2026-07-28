"""Tests P0 — Webhook Stripe : payment_failed, charge.dispute.created, charge.refunded.

Couvre les fixes FF-01 :
  - handle_payment_failure : marque payment + order en failed, idempotent
  - handle_charge_refunded : sync refunded / partially_refunded
  - _handle_dispute_alert : log CRITICAL, pas de crash
  - handle_webhook : dispatch correct vers chaque handler

Tous les tests sont des tests unitaires (AsyncMock session) sans DB réelle.
"""

import logging
from unittest.mock import AsyncMock, patch


from app.modules.orders.models import Order
from app.modules.payments import service
from app.modules.payments.models import Payment


# ---------------------------------------------------------------------------
# handle_payment_failure
# ---------------------------------------------------------------------------


async def test_handle_payment_failure_marks_payment_and_order_failed():
    """payment_intent.payment_failed → payment.status=failed + order.payment_status=failed.

    [⚠️ PROD] Sans ce handler, un paiement refusé par la banque resterait en
    statut "pending" et l'ordre pourrait être traité comme validé.
    """
    payment = Payment(id=1, order_id=10, provider_payment_id="pi_test", status="pending")
    order = Order(id=10, status="pending", payment_status="pending", total=50)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.get = AsyncMock(return_value=order)
    session.commit = AsyncMock()

    await service.handle_payment_failure(session, "pi_test")

    assert payment.status == "failed"
    assert order.payment_status == "failed"
    session.commit.assert_awaited_once()


async def test_handle_payment_failure_idempotent_if_already_paid():
    """Paiement déjà en status paid → aucune modification, pas de commit.

    [⚠️ PROD] Stripe peut rejouer les webhooks. L'idempotence est critique pour
    éviter de marquer failed un paiement déjà confirmé.
    """
    payment = Payment(id=1, order_id=10, provider_payment_id="pi_test", status="paid")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    await service.handle_payment_failure(session, "pi_test")

    assert payment.status == "paid"
    session.commit.assert_not_awaited()


async def test_handle_payment_failure_idempotent_if_already_failed():
    """Paiement déjà failed → pas de double commit."""
    payment = Payment(id=1, order_id=10, provider_payment_id="pi_dup", status="failed")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    await service.handle_payment_failure(session, "pi_dup")

    session.commit.assert_not_awaited()


async def test_handle_payment_failure_noop_if_payment_not_found():
    """PI inconnu → retour silencieux, pas de crash, pas de commit."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    await service.handle_payment_failure(session, "pi_unknown")

    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_charge_refunded
# ---------------------------------------------------------------------------


async def test_handle_charge_refunded_full_sets_refunded():
    """charge.refunded total (amount == amount_refunded) → payment.status = refunded."""
    payment = Payment(id=2, order_id=20, provider_payment_id="pi_ref", status="paid")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    charge = {"amount": 1000, "amount_refunded": 1000, "payment_intent": "pi_ref"}
    await service.handle_charge_refunded(session, "pi_ref", charge)

    assert payment.status == "refunded"
    session.commit.assert_awaited_once()


async def test_handle_charge_refunded_partial_sets_partially_refunded():
    """charge.refunded partiel → payment.status = partially_refunded."""
    payment = Payment(id=2, order_id=20, provider_payment_id="pi_ref", status="paid")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    charge = {"amount": 1000, "amount_refunded": 400, "payment_intent": "pi_ref"}
    await service.handle_charge_refunded(session, "pi_ref", charge)

    assert payment.status == "partially_refunded"
    session.commit.assert_awaited_once()


async def test_handle_charge_refunded_idempotent_if_already_refunded():
    """Paiement déjà refunded → pas de commit redondant."""
    payment = Payment(id=2, order_id=20, provider_payment_id="pi_ref", status="refunded")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    charge = {"amount": 1000, "amount_refunded": 1000}
    await service.handle_charge_refunded(session, "pi_ref", charge)

    session.commit.assert_not_awaited()


async def test_handle_charge_refunded_noop_if_zero_amount():
    """Charge avec amount_refunded=0 → aucun changement de statut."""
    payment = Payment(id=2, order_id=20, provider_payment_id="pi_ref", status="paid")

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=payment)
    session.commit = AsyncMock()

    charge = {"amount": 1000, "amount_refunded": 0}
    await service.handle_charge_refunded(session, "pi_ref", charge)

    assert payment.status == "paid"
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_dispute_alert
# ---------------------------------------------------------------------------


async def test_handle_dispute_alert_emits_critical_log(caplog):
    """charge.dispute.created → log CRITICAL avec dispute_id et tenant_slug.

    [⚠️ PROD] Ce log est le seul signal d'alerte — surveiller en prod via Sentry
    ou alerting sur niveau CRITICAL.
    """
    dispute = {
        "id": "dp_test123",
        "amount": 2500,
        "currency": "eur",
        "reason": "fraudulent",
        "payment_intent": "pi_test",
    }

    with caplog.at_level(logging.CRITICAL, logger="app.modules.payments.service"):
        await service._handle_dispute_alert("acme-restaurant", dispute)

    assert any("dp_test123" in record.message for record in caplog.records), (
        "Le dispute_id doit apparaître dans le log CRITICAL"
    )
    assert any(record.levelno == logging.CRITICAL for record in caplog.records), (
        "Le log doit être au niveau CRITICAL"
    )
    assert any("acme-restaurant" in record.message for record in caplog.records), (
        "Le tenant_slug doit apparaître dans le log"
    )


async def test_handle_dispute_alert_does_not_raise():
    """_handle_dispute_alert ne lève aucune exception (pas de side-effect DB)."""
    await service._handle_dispute_alert("tenant", {})  # objet dispute vide — pas de crash


# ---------------------------------------------------------------------------
# handle_webhook dispatch
# ---------------------------------------------------------------------------


async def test_handle_webhook_dispatches_payment_failed():
    """payment_intent.payment_failed → handle_payment_failure appelé avec le bon PI."""
    session = AsyncMock()
    payload = {
        "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_abc"}},
    }

    with patch.object(service, "handle_payment_failure", new_callable=AsyncMock) as mock_fail:
        await service.handle_webhook(session, "acme", payload)

    mock_fail.assert_awaited_once_with(session, "pi_abc")


async def test_handle_webhook_dispatches_payment_canceled():
    """payment_intent.canceled → handle_payment_failure appelé (même handler que failed)."""
    session = AsyncMock()
    payload = {
        "type": "payment_intent.canceled",
        "data": {"object": {"id": "pi_cancel"}},
    }

    with patch.object(service, "handle_payment_failure", new_callable=AsyncMock) as mock_fail:
        await service.handle_webhook(session, "acme", payload)

    mock_fail.assert_awaited_once_with(session, "pi_cancel")


async def test_handle_webhook_dispatches_dispute():
    """charge.dispute.created → _handle_dispute_alert appelé avec tenant_slug et objet dispute."""
    session = AsyncMock()
    dispute_obj = {"id": "dp_xyz", "amount": 5000}
    payload = {
        "type": "charge.dispute.created",
        "data": {"object": dispute_obj},
    }

    with patch.object(service, "_handle_dispute_alert", new_callable=AsyncMock) as mock_alert:
        await service.handle_webhook(session, "my-tenant", payload)

    mock_alert.assert_awaited_once_with("my-tenant", dispute_obj)


async def test_handle_webhook_dispatches_charge_refunded():
    """charge.refunded → handle_charge_refunded appelé avec le PI extrait du charge object."""
    session = AsyncMock()
    charge_obj = {"payment_intent": "pi_refund", "amount": 1000, "amount_refunded": 1000}
    payload = {
        "type": "charge.refunded",
        "data": {"object": charge_obj},
    }

    with patch.object(service, "handle_charge_refunded", new_callable=AsyncMock) as mock_ref:
        await service.handle_webhook(session, "acme", payload)

    mock_ref.assert_awaited_once_with(session, "pi_refund", charge_obj)


async def test_handle_webhook_unknown_event_does_not_raise():
    """Event inconnu → log debug, pas d'exception levée."""
    session = AsyncMock()
    payload = {
        "type": "customer.subscription.updated",
        "data": {"object": {}},
    }

    await service.handle_webhook(session, "acme", payload)  # ne doit pas crasher
