"""Phase 2 livraison interne : empreinte bancaire (garantie de paiement) pour la livraison.

Stripe est simule (``_FakeStripe``) ; la base est reelle (savepoint rollbackee par test).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import stripe

from app.core.http.errors import AppError
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments import guarantee, service
from app.modules.payments.models import Payment

USER_ID = 7
_event_counter = 0


class _FakeStripe:
    """Memoire minimale d'un PaymentIntent a capture manuelle."""

    def __init__(self):
        self.intents: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_cancel = False

    def create(self, **kwargs):
        pid = f"pi_guar_{len(self.intents) + 1}"
        self.intents[pid] = {
            "id": pid,
            "client_secret": f"{pid}_secret",
            "status": "requires_payment_method",
            "capture_method": kwargs.get("capture_method"),
            "amount": kwargs["amount"],
            "amount_capturable": 0,
            "currency": kwargs["currency"],
            "metadata": dict(kwargs["metadata"]),
        }
        self.calls.append(("create", kwargs))
        return dict(self.intents[pid])

    def authorize(self, pid):
        intent = self.intents[pid]
        intent.update(status="requires_capture", amount_capturable=intent["amount"])

    def retrieve(self, pid, **kwargs):
        return dict(self.intents[pid])

    def capture(self, pid, amount_to_capture=None, **kwargs):
        self.calls.append(("capture", pid, amount_to_capture))
        intent = self.intents[pid]
        intent.update(status="succeeded", amount_received=amount_to_capture or intent["amount"])
        return dict(intent)

    def cancel(self, pid, **kwargs):
        self.calls.append(("cancel", pid))
        if self.fail_cancel:
            raise stripe.error.APIConnectionError("network down")
        self.intents[pid]["status"] = "canceled"
        return dict(self.intents[pid])

    def cancels(self):
        return [c for c in self.calls if c[0] == "cancel"]

    def captures(self):
        return [c for c in self.calls if c[0] == "capture"]


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = _FakeStripe()
    monkeypatch.setattr(stripe.PaymentIntent, "create", fake.create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", fake.retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, "capture", fake.capture)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel", fake.cancel)
    return fake


async def _order(session, *, order_type="delivery", total=25.0, user_id=USER_ID, status="pending"):
    order = Order(
        user_id=user_id,
        status=status,
        payment_status="pending",
        order_type=order_type,
        total=total,
        subtotal=total,
        delivery_address="1 rue de la Paix, Paris" if order_type == "delivery" else None,
    )
    session.add(order)
    await session.flush()
    return order


async def _hold(session, slug, fake, order, *, via_webhook=True):
    """Pose une empreinte complete : intent -> carte autorisee -> finalisation."""
    created = await guarantee.create_guarantee_intent(
        session,
        order.id,
        slug,
        USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION,
        accept_terms=True,
    )
    payment = created["payment"]
    fake.authorize(payment.provider_payment_id)
    if via_webhook:
        await service.handle_webhook(session, slug, _event("payment_intent.amount_capturable_updated", fake, payment))
    return payment


def _event(event_type, fake, payment, **intent_overrides):
    global _event_counter
    _event_counter += 1
    intent = dict(fake.intents[payment.provider_payment_id])
    intent.update(intent_overrides)
    return {"id": f"evt_guar_{_event_counter}", "type": event_type, "data": {"object": intent}}


# --------------------------------------------------------------------------- creation


async def test_guarantee_intent_requires_accepted_current_terms(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    with pytest.raises(AppError) as refused:
        await guarantee.create_guarantee_intent(
            db_session, order.id, demo_tenant_slug, USER_ID,
            terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=False,
        )
    assert refused.value.code == "GUARANTEE_TERMS_NOT_ACCEPTED"

    with pytest.raises(AppError) as outdated:
        await guarantee.create_guarantee_intent(
            db_session, order.id, demo_tenant_slug, USER_ID,
            terms_version="1999-01-01", accept_terms=True,
        )
    assert outdated.value.code == "GUARANTEE_TERMS_OUTDATED"
    assert fake_stripe.calls == []


async def test_guarantee_intent_is_manual_capture_and_traces_terms(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    result = await guarantee.create_guarantee_intent(
        db_session, order.id, demo_tenant_slug, USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
    )
    payment = result["payment"]
    (_, kwargs) = fake_stripe.calls[0]
    assert kwargs["capture_method"] == "manual"
    assert kwargs["amount"] == 2500
    assert kwargs["metadata"]["purpose"] == "guarantee"
    assert kwargs["metadata"]["order_id"] == str(order.id)
    assert payment.purpose == "guarantee"
    assert payment.status == "pending"
    assert payment.guarantee_terms_version == guarantee.GUARANTEE_TERMS_VERSION
    assert payment.guarantee_terms_accepted_at is not None
    assert result["client_secret"].endswith("_secret")

    # Rejouer la requete reutilise l'intent au lieu d'en creer un second.
    again = await guarantee.create_guarantee_intent(
        db_session, order.id, demo_tenant_slug, USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
    )
    assert again["payment"].id == payment.id
    assert len(fake_stripe.calls) == 1


async def test_guarantee_intent_is_delivery_only_and_owner_only(db_session, demo_tenant_slug, fake_stripe):
    pickup = await _order(db_session, order_type="pickup")
    with pytest.raises(AppError) as only_delivery:
        await guarantee.create_guarantee_intent(
            db_session, pickup.id, demo_tenant_slug, USER_ID,
            terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
        )
    assert only_delivery.value.code == "GUARANTEE_DELIVERY_ONLY"

    delivery = await _order(db_session)
    with pytest.raises(AppError) as stranger:
        await guarantee.create_guarantee_intent(
            db_session, delivery.id, demo_tenant_slug, USER_ID + 1,
            terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
        )
    assert stranger.value.status_code == 404
    assert fake_stripe.calls == []


# --------------------------------------------------------------------------- finalisation


async def test_webhook_authorizes_guarantee_and_confirms_order(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    payment = await _hold(db_session, demo_tenant_slug, fake_stripe, order)

    refreshed = await db_session.get(Payment, payment.id)
    await db_session.refresh(order)
    assert refreshed.status == "authorized"
    assert order.payment_status == "guaranteed"
    assert order.status == "confirmed"

    # Rejeu (autre event.id, meme etat) : aucun effet de bord.
    await service.handle_webhook(
        db_session, demo_tenant_slug, _event("payment_intent.amount_capturable_updated", fake_stripe, payment)
    )
    await db_session.refresh(order)
    assert order.status == "confirmed"


async def test_confirm_endpoint_path_finalizes_guarantee(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    payment = await _hold(db_session, demo_tenant_slug, fake_stripe, order, via_webhook=False)

    confirmed = await service.confirm(
        db_session, payment.provider_payment_id, tenant_slug=demo_tenant_slug, user_id=USER_ID
    )
    assert confirmed.status == "authorized"
    await db_session.refresh(order)
    assert order.payment_status == "guaranteed"


async def test_confirm_refuses_guarantee_not_really_authorized(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    created = await guarantee.create_guarantee_intent(
        db_session, order.id, demo_tenant_slug, USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
    )
    # Le client n'a jamais valide sa carte : Stripe repond requires_payment_method.
    with pytest.raises(AppError) as refused:
        await service.confirm(
            db_session, created["payment"].provider_payment_id, tenant_slug=demo_tenant_slug, user_id=USER_ID
        )
    assert refused.value.code == "GUARANTEE_NOT_AUTHORIZED"
    await db_session.refresh(order)
    assert order.payment_status == "pending"
    assert order.status == "pending"


async def test_finalize_rejects_forged_intent_data(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    created = await guarantee.create_guarantee_intent(
        db_session, order.id, demo_tenant_slug, USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
    )
    payment = created["payment"]
    fake_stripe.authorize(payment.provider_payment_id)

    for overrides, code in [
        ({"amount": 100, "amount_capturable": 100}, "PAYMENT_AMOUNT_MISMATCH"),
        ({"amount_capturable": 100}, "GUARANTEE_AMOUNT_NOT_HELD"),
        ({"capture_method": "automatic"}, "GUARANTEE_CAPTURE_METHOD_INVALID"),
        ({"currency": "usd"}, "PAYMENT_CURRENCY_MISMATCH"),
        ({"metadata": {"purpose": "guarantee"}}, "PAYMENT_METADATA_MISMATCH"),
    ]:
        event = _event("payment_intent.amount_capturable_updated", fake_stripe, payment, **overrides)
        with pytest.raises(AppError) as refused:
            await service.handle_webhook(db_session, demo_tenant_slug, event)
        assert refused.value.code == code, overrides
    await db_session.refresh(order)
    assert order.payment_status == "pending"


async def test_checkout_link_guarantee_is_finalized_by_metadata(db_session, demo_tenant_slug, fake_stripe):
    """Lien de paiement : le paiement local porte l'id de session cs_..., le PaymentIntent n'est
    connu qu'au webhook. Il est retrouve par ses metadonnees puis rattache."""
    order = await _order(db_session, total=25.0)
    payment = Payment(
        order_id=order.id, provider="stripe", provider_payment_id="cs_test_123", purpose="guarantee",
        amount=25.0, currency="EUR", status="pending",
    )
    db_session.add(payment)
    await db_session.flush()
    fake_stripe.intents["pi_link_1"] = {
        "id": "pi_link_1", "status": "requires_capture", "capture_method": "manual",
        "amount": 2500, "amount_capturable": 2500, "currency": "eur",
        "metadata": {
            "tenant_slug": demo_tenant_slug, "order_id": str(order.id),
            "payment_id": str(payment.id), "purpose": "guarantee",
        },
    }
    event = {
        "id": "evt_link_1", "type": "payment_intent.amount_capturable_updated",
        "data": {"object": dict(fake_stripe.intents["pi_link_1"])},
    }
    await service.handle_webhook(db_session, demo_tenant_slug, event)

    refreshed = await db_session.get(Payment, payment.id)
    assert refreshed.provider_payment_id == "pi_link_1"
    assert refreshed.status == "authorized"
    assert refreshed.guarantee_terms_version == guarantee.GUARANTEE_TERMS_VERSION
    await db_session.refresh(order)
    assert order.payment_status == "guaranteed"


# --------------------------------------------------------------------------- reglement


async def test_delivered_is_refused_until_guarantee_is_settled(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    order.status = "out_for_delivery"
    await db_session.flush()

    with pytest.raises(AppError) as blocked:
        await orders_service.update_status(db_session, order.id, "delivered", tenant_slug=demo_tenant_slug)
    assert blocked.value.code == "PAYMENT_SETTLEMENT_REQUIRED"


async def test_cash_collected_releases_hold_and_records_cash_payment(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    order.status = "out_for_delivery"
    await db_session.flush()

    with pytest.raises(AppError) as short:
        await guarantee.settle_guarantee_cash(
            db_session, demo_tenant_slug, order.id, amount_received=10, user_id=3
        )
    assert short.value.code == "CASH_AMOUNT_INSUFFICIENT"

    cash = await guarantee.settle_guarantee_cash(
        db_session, demo_tenant_slug, order.id, amount_received=30, user_id=3
    )
    assert cash.provider == "cash" and cash.status == "paid" and float(cash.amount) == 25.0
    assert float(cash.amount_received) == 30.0
    assert fake_stripe.cancels() == [("cancel", hold.provider_payment_id)]
    assert (await db_session.get(Payment, hold.id)).status == "released"
    await db_session.refresh(order)
    assert order.payment_status == "paid"

    delivered = await orders_service.update_status(db_session, order.id, "delivered", tenant_slug=demo_tenant_slug)
    assert delivered.status == "delivered"

    # Plus aucune empreinte active : un deuxieme reglement est refuse.
    with pytest.raises(AppError) as twice:
        await guarantee.settle_guarantee_cash(db_session, demo_tenant_slug, order.id, amount_received=30, user_id=3)
    assert twice.value.code == "GUARANTEE_NOT_FOUND"


async def test_partial_capture_debits_only_requested_amount(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)

    with pytest.raises(AppError) as no_reason:
        await guarantee.capture_guarantee(
            db_session, demo_tenant_slug, order.id, amount_cents=500, reason="  ", user_id=1
        )
    assert no_reason.value.code == "CAPTURE_REASON_REQUIRED"
    with pytest.raises(AppError) as too_much:
        await guarantee.capture_guarantee(
            db_session, demo_tenant_slug, order.id, amount_cents=2501, reason="client absent", user_id=1
        )
    assert too_much.value.code == "INVALID_CAPTURE_AMOUNT"
    assert fake_stripe.captures() == []

    captured = await guarantee.capture_guarantee(
        db_session, demo_tenant_slug, order.id, amount_cents=500, reason="client absent", user_id=1
    )
    assert fake_stripe.captures() == [("capture", hold.provider_payment_id, 500)]
    assert captured.status == "paid" and float(captured.captured_amount) == 5.0
    assert captured.settlement_note == "client absent"
    await db_session.refresh(order)
    assert order.payment_status == "guarantee_captured"

    # Comptabilite : on encaisse 5 EUR, pas 25.
    detail = await service.get_payment_for_order(db_session, order.id, user_id=USER_ID, is_staff=True, include_receipt=False)
    assert detail.paid_amount_cents == 500
    assert detail.remaining_refundable_cents == 500


async def test_full_capture_marks_order_paid(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    await guarantee.capture_guarantee(
        db_session, demo_tenant_slug, order.id, amount_cents=None, reason="paiement carte a la livraison", user_id=1
    )
    await db_session.refresh(order)
    assert order.payment_status == "paid"
    assert fake_stripe.captures()[0][2] == 2500


async def test_release_is_idempotent_safe_and_leaves_hold_if_stripe_fails(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    hold_id, order_id = hold.id, order.id  # les objets sont expires par le rollback attendu

    fake_stripe.fail_cancel = True
    with pytest.raises(AppError) as failed:
        await guarantee.release_guarantee(db_session, demo_tenant_slug, order_id, reason="x", user_id=1)
    assert failed.value.code == "STRIPE_RELEASE_FAILED"
    assert (await db_session.get(Payment, hold_id)).status == "authorized"  # jamais « libere » a tort

    fake_stripe.fail_cancel = False
    released = await guarantee.release_guarantee(db_session, demo_tenant_slug, order_id, reason="ok", user_id=1)
    assert released.status == "released"
    await db_session.refresh(order)
    assert order.payment_status == "guarantee_released"

    with pytest.raises(AppError) as again:
        await guarantee.release_guarantee(db_session, demo_tenant_slug, order_id, reason="ok", user_id=1)
    assert again.value.code == "GUARANTEE_NOT_FOUND"


async def test_cancelling_the_order_releases_the_hold(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)

    await orders_service.update_status(db_session, order.id, "cancelled", tenant_slug=demo_tenant_slug)

    assert fake_stripe.cancels() == [("cancel", hold.provider_payment_id)]
    assert (await db_session.get(Payment, hold.id)).status == "released"
    await db_session.refresh(order)
    assert order.status == "cancelled"
    assert order.payment_status == "guarantee_released"


async def test_cancel_still_succeeds_when_release_fails(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    hold_id = hold.id
    fake_stripe.fail_cancel = True

    await orders_service.update_status(db_session, order.id, "cancelled", tenant_slug=demo_tenant_slug)

    await db_session.refresh(order)
    assert order.status == "cancelled"  # le flux metier n'est jamais bloque
    assert (await db_session.get(Payment, hold_id)).status == "authorized"  # expirera cote Stripe (7 j)


# --------------------------------------------------------------------------- webhooks de fin de vie


async def test_canceled_webhook_on_active_hold_is_a_release_not_a_failure(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)

    await service.handle_webhook(
        db_session, demo_tenant_slug, _event("payment_intent.canceled", fake_stripe, hold, status="canceled")
    )

    refreshed = await db_session.get(Payment, hold.id)
    assert refreshed.status == "released"
    assert refreshed.settlement_note == "released_by_stripe"
    await db_session.refresh(order)
    assert order.payment_status == "guarantee_released"


async def test_echo_of_our_own_cancel_does_not_turn_release_into_failure(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    await guarantee.release_guarantee(db_session, demo_tenant_slug, order.id, reason="staff", user_id=1)

    await service.handle_webhook(
        db_session, demo_tenant_slug, _event("payment_intent.canceled", fake_stripe, hold, status="canceled")
    )

    assert (await db_session.get(Payment, hold.id)).status == "released"
    await db_session.refresh(order)
    assert order.payment_status == "guarantee_released"


async def test_capture_succeeded_webhook_echo_is_ignored(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session, total=25.0)
    hold = await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    await guarantee.capture_guarantee(
        db_session, demo_tenant_slug, order.id, amount_cents=500, reason="client absent", user_id=1
    )

    await service.handle_webhook(
        db_session, demo_tenant_slug, _event("payment_intent.succeeded", fake_stripe, hold)
    )

    refreshed = await db_session.get(Payment, hold.id)
    assert refreshed.status == "paid" and float(refreshed.captured_amount) == 5.0
    await db_session.refresh(order)
    assert order.payment_status == "guarantee_captured"


# --------------------------------------------------------------------------- isolation du flux classique


async def test_pay_now_intent_refuses_an_already_guaranteed_order(db_session, demo_tenant_slug, fake_stripe):
    order = await _order(db_session)
    await _hold(db_session, demo_tenant_slug, fake_stripe, order)
    with pytest.raises(AppError) as refused:
        await service.create_intent(db_session, order.id, tenant_slug=demo_tenant_slug, user_id=USER_ID)
    assert refused.value.code == "ORDER_ALREADY_PAID"


async def test_pay_now_intent_ignores_a_pending_guarantee(db_session, demo_tenant_slug, fake_stripe):
    """Un client qui change d'avis (payer maintenant) ne doit pas recevoir le client_secret d'une
    empreinte a capture manuelle."""
    order = await _order(db_session)
    created = await guarantee.create_guarantee_intent(
        db_session, order.id, demo_tenant_slug, USER_ID,
        terms_version=guarantee.GUARANTEE_TERMS_VERSION, accept_terms=True,
    )
    sale = await service.create_intent(db_session, order.id, tenant_slug=demo_tenant_slug, user_id=USER_ID)
    assert sale["payment"].id != created["payment"].id
    assert sale["payment"].purpose == "sale"
    assert fake_stripe.calls[-1][1].get("capture_method") is None


async def test_confirmation_needs_paid_or_guaranteed_delivery(db_session, demo_tenant_slug):
    pending = await _order(db_session)
    with pytest.raises(AppError) as unpaid:
        await orders_service.update_status(db_session, pending.id, "confirmed", tenant_slug=demo_tenant_slug)
    assert unpaid.value.code == "PAYMENT_REQUIRED"

    pickup = await _order(db_session, order_type="pickup")
    pickup.payment_status = "guaranteed"  # etat impossible par l'API : defense en profondeur
    await db_session.flush()
    with pytest.raises(AppError) as wrong_type:
        await orders_service.update_status(db_session, pickup.id, "confirmed", tenant_slug=demo_tenant_slug)
    assert wrong_type.value.code == "PAYMENT_REQUIRED"


# --------------------------------------------------------------------------- comptoir : lien de paiement


async def _manual(monkeypatch, *, order_type, payment, link=None):
    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock()
    order = Order(id=91, status="pending", payment_status="pending", order_type=order_type, total=24)
    monkeypatch.setattr(orders_service, "create_order", AsyncMock(return_value=order))
    monkeypatch.setattr(orders_service, "_serialize_order_detail", AsyncMock(return_value={"id": 91}))
    monkeypatch.setattr(orders_service, "build_receipt", AsyncMock(return_value={"order_id": 91}))
    link_mock = AsyncMock(side_effect=link) if isinstance(link, Exception) else AsyncMock(return_value=link)
    monkeypatch.setattr(guarantee, "create_payment_link", link_mock)
    body = ManualOrderCreate(
        order_type=order_type,
        delivery_address="1 rue X" if order_type == "delivery" else None,
        items=[OrderItemCreate(product_id=1, quantity=1)],
        payment=payment,
    )
    result = await orders_service.create_manual_order(
        session, body, actor_user_id=4, tenant_slug="test", idempotency_key="k-link"
    )
    return result, link_mock, order


async def test_manual_delivery_cash_is_refused(monkeypatch):
    with pytest.raises(AppError) as refused:
        await _manual(monkeypatch, order_type="delivery", payment={"method": "cash", "amount_received": 30})
    assert refused.value.code == "DELIVERY_CASH_REQUIRES_GUARANTEE"


async def test_manual_delivery_payment_link_defaults_to_guarantee(monkeypatch):
    link = {"url": "https://pay.example/x", "expires_at": None, "mode": "guarantee", "payment_id": 5}
    result, link_mock, order = await _manual(
        monkeypatch, order_type="delivery", payment={"method": "payment_link"}, link=link
    )
    assert link_mock.await_args.kwargs["mode"] == "guarantee"
    assert result["payment"] is None and result["payment_link"] == link
    assert order.status == "pending" and order.payment_status == "pending"  # confirme par le webhook


async def test_manual_pickup_payment_link_is_full_payment_and_guarantee_is_refused(monkeypatch):
    result, link_mock, _ = await _manual(
        monkeypatch, order_type="pickup", payment={"method": "payment_link"},
        link={"url": "u", "mode": "full", "payment_id": 6},
    )
    assert link_mock.await_args.kwargs["mode"] == "full"
    with pytest.raises(AppError) as refused:
        await _manual(monkeypatch, order_type="pickup", payment={"method": "payment_link", "link_mode": "guarantee"})
    assert refused.value.code == "GUARANTEE_DELIVERY_ONLY"


async def test_manual_order_survives_a_failing_payment_link(monkeypatch):
    result, _, order = await _manual(
        monkeypatch, order_type="delivery", payment={"method": "payment_link"},
        link=AppError("STRIPE_PAYMENT_FAILED", "Stripe down", 502),
    )
    assert result["payment_link"] is None
    assert result["payment_link_error"] == "STRIPE_PAYMENT_FAILED"
    assert result["order"] == {"id": 91}


async def test_manual_dine_in_payment_link_is_refused_before_creating_the_order(monkeypatch):
    create_order = AsyncMock()
    monkeypatch.setattr(orders_service, "create_order", create_order)
    with pytest.raises(AppError) as refused:
        await _manual(monkeypatch, order_type="dine_in", payment={"method": "payment_link"})
    assert refused.value.code == "PAYMENT_LINK_UNSUPPORTED"


def test_link_mode_is_only_valid_with_payment_link():
    from app.modules.orders.schemas import ManualOrderPaymentCreate

    with pytest.raises(ValueError):
        ManualOrderPaymentCreate(method="cash", link_mode="full")


# --------------------------------------------------------------------------- HTTP


async def test_public_link_done_page_needs_no_auth_and_is_not_cached(client):
    response = await client.get("/api/v1/payments/public/link-done?status=cancelled")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "Aucun debit" in response.text

    # Un statut inconnu ne reflechit jamais l'entree dans la page (pas d'XSS).
    hostile = await client.get("/api/v1/payments/public/link-done", params={"status": "<b>x</b>"})
    assert hostile.status_code == 200 and "<b>x" not in hostile.text


async def test_guarantee_routes_require_authentication(client):
    assert (await client.get("/api/v1/payments/guarantee-terms")).status_code == 401
    assert (await client.post("/api/v1/payments/guarantee-intent", json={})).status_code == 401
    for path in ("guarantee/release", "guarantee/capture", "guarantee/cash-collected", "link"):
        assert (await client.post(f"/api/v1/payments/1/{path}", json={})).status_code == 401, path


async def test_guarantee_terms_are_served_not_shadowed_by_order_route(authed_client):
    response = await authed_client.get("/api/v1/payments/guarantee-terms")
    assert response.status_code == 200
    assert response.json()["version"] == guarantee.GUARANTEE_TERMS_VERSION
    assert response.json()["hold_validity_days"] == 7
