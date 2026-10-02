"""Phase 4 livraison interne : preuve de remise par code, echecs, traitement admin.

Base reelle (savepoint rollbackee par test). Les helpers committent apres leur preparation : les
services testes font des ``rollback()`` pour liberer leurs verrous.
"""

import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.auth.security import create_access_token
from app.core.http.errors import AppError
from app.modules.delivery import dispatch_service as svc
from app.modules.delivery import failures, proof
from app.modules.delivery import service as delivery_service
from app.modules.delivery.models import (
    Delivery,
    DeliveryCodeAttempt,
    DeliveryEvent,
    DeliveryFailure,
    DriverProfile,
)
from app.modules.hr.models import EmployeeProfile, Establishment, TimeClockEntry
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments.models import Payment


async def _establishment(session):
    est = Establishment(name="Resto P4", timezone="Europe/Paris", is_active=True)
    session.add(est)
    await session.commit()
    # Simple identifiant : l'objet ORM serait expire par les rollbacks des services testes.
    return SimpleNamespace(id=est.id)


async def _driver(session, est):
    out = await svc.create_driver(
        session,
        email=f"p4-{uuid.uuid4().hex[:8]}@test.fr",
        full_name="Marc Livreur",
        phone=None,
        vehicle=None,
        establishment_id=est.id,
    )
    emp = await session.scalar(select(EmployeeProfile).where(EmployeeProfile.user_id == out["user_id"]))
    session.add(
        TimeClockEntry(
            employee_id=emp.id,
            establishment_id=est.id,
            clock_in_at=datetime.now(timezone.utc),
            method="web",
            status="open",
        )
    )
    await session.commit()
    return out


async def _order(session, est, *, status="ready", payment_status="paid", total=25.0, user_id=None, order_type="delivery"):
    order = Order(
        user_id=user_id,
        establishment_id=est.id,
        status=status,
        payment_status=payment_status,
        order_type=order_type,
        total=total,
        subtotal=total,
        customer_name="Claire D.",
        customer_phone="0698765432",
        delivery_address="1 rue de la Paix, Paris",
    )
    session.add(order)
    await session.commit()
    return order


async def _settings(session, **values):
    row = await delivery_service.get_delivery_settings(session)
    for key, value in values.items():
        setattr(row, key, value)
    await session.commit()


async def _profile(session, driver):
    return await session.get(DriverProfile, driver["id"], populate_existing=True)


async def _en_route(session, slug, est, driver, *, arrived=True, **order_kwargs):
    """Une commande attribuee, partie, et (par defaut) signalee arrivee. Renvoie (order_id, delivery_id)."""
    order = await _order(session, est, **order_kwargs)
    order_id = order.id
    await svc.assign(session, order_ids=[order_id], driver_id=driver["id"], actor_user_id=1, tenant_slug=slug)
    delivery_id = await session.scalar(select(Delivery.id).where(Delivery.order_id == order_id))
    kwargs = {"user_id": driver["user_id"], "tenant_slug": slug}
    await svc.driver_depart(session, await _profile(session, driver), [delivery_id], **kwargs)
    if arrived:
        await svc.driver_arrived(session, await _profile(session, driver), delivery_id, **kwargs)
    return order_id, delivery_id


async def _deliver(session, slug, driver, delivery_id, **kwargs):
    return await svc.driver_deliver(
        session,
        await _profile(session, driver),
        delivery_id,
        user_id=driver["user_id"],
        tenant_slug=slug,
        cash_received=kwargs.pop("cash_received", None),
        **kwargs,
    )


async def _code(awaitable) -> str:
    with pytest.raises(AppError) as err:
        await awaitable
    return err.value.code


async def _hold(session, order_id, total=25.0):
    session.add(
        Payment(
            order_id=order_id, provider="stripe", provider_payment_id=f"local_{uuid.uuid4().hex[:6]}",
            purpose="guarantee", amount=total, currency="EUR", status="authorized",
        )
    )
    await session.commit()


# --------------------------------------------------------------------------- le code


def test_code_is_four_digits_deterministic_and_bound_to_order_and_nonce():
    code = proof.derive_code("pizza", 7, "abc")
    assert len(code) == 4 and code.isdigit()
    assert proof.derive_code("pizza", 7, "abc") == code
    variants = {proof.derive_code("pizza", 8, "abc"), proof.derive_code("other", 7, "abc"), proof.derive_code("pizza", 7, "abd")}
    assert len(variants | {code}) > 1  # pas un code constant


async def test_code_is_stable_never_stored_and_changes_with_the_nonce(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    order = await _order(db_session, est)
    order_id = order.id

    first = await proof.get_code(db_session, demo_tenant_slug, order_id)
    assert await proof.get_code(db_session, demo_tenant_slug, order_id) == first
    nonce = await db_session.scalar(select(Order.delivery_code_nonce).where(Order.id == order_id))
    assert nonce and first not in nonce  # la base ne contient que la graine

    await proof.regenerate_nonce(db_session, order_id)
    await db_session.commit()
    new_nonce = await db_session.scalar(select(Order.delivery_code_nonce).where(Order.id == order_id))
    assert new_nonce != nonce


async def test_customer_gets_the_code_only_for_own_active_delivery(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    mine = await _order(db_session, est, user_id=5, status="preparing")
    pickup = await _order(db_session, est, user_id=5, order_type="pickup")
    done = await _order(db_session, est, user_id=5, status="delivered")
    ids = (mine.id, pickup.id, done.id)

    got = await orders_service.get_delivery_code(db_session, ids[0], 5, demo_tenant_slug)
    assert got["length"] == 4 and got["code"] == await proof.get_code(db_session, demo_tenant_slug, ids[0])
    assert await _code(orders_service.get_delivery_code(db_session, ids[0], 6, demo_tenant_slug)) == "ORDER_NOT_FOUND"
    assert await _code(orders_service.get_delivery_code(db_session, ids[1], 5, demo_tenant_slug)) == "ORDER_NOT_DELIVERY"
    assert await _code(orders_service.get_delivery_code(db_session, ids[2], 5, demo_tenant_slug)) == "ORDER_NOT_ACTIVE"


# --------------------------------------------------------------------------- preuve a la livraison


async def test_with_proof_required_the_driver_needs_the_right_code(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _settings(db_session, delivery_proof_required=True)
    driver = await _driver(db_session, est)
    order_id, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver)

    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id)) == "DELIVERY_CODE_REQUIRED"
    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code="12")) == "DELIVERY_CODE_INVALID"
    good = await proof.get_code(db_session, demo_tenant_slug, order_id)
    wrong = "0000" if good != "0000" else "1111"
    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code=wrong)) == "DELIVERY_CODE_INVALID"
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.status == "out_for_delivery"

    done = await _deliver(db_session, demo_tenant_slug, driver, delivery_id, code=good)
    assert done.status == "delivered"
    attempts = (await db_session.execute(select(DeliveryCodeAttempt).where(DeliveryCodeAttempt.delivery_id == delivery_id).order_by(DeliveryCodeAttempt.id))).scalars().all()
    assert [a.success for a in attempts] == [False, True]  # le format invalide n'est pas un essai
    event = await db_session.scalar(select(DeliveryEvent).where(DeliveryEvent.order_id == order_id, DeliveryEvent.event == "delivered"))
    assert event.note == "proof:code"


async def test_five_wrong_codes_lock_the_delivery_even_for_the_right_code(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _settings(db_session, delivery_proof_required=True)
    driver = await _driver(db_session, est)
    order_id, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver)
    good = await proof.get_code(db_session, demo_tenant_slug, order_id)
    wrong = "0000" if good != "0000" else "1111"

    for _ in range(proof.MAX_FAILED_ATTEMPTS):
        assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code=wrong)) == "DELIVERY_CODE_INVALID"
    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code=good)) == "DELIVERY_CODE_LOCKED"
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.status == "out_for_delivery"
    assert await proof.failed_attempts(db_session, delivery_id) == proof.MAX_FAILED_ATTEMPTS

    # Seul un administrateur peut conclure, avec un motif.
    done = await svc.admin_deliver_without_code(
        db_session, order_id=order_id, reason="Client verifie par telephone", admin_user_id=1, tenant_slug=demo_tenant_slug
    )
    assert done.status == "delivered"


async def test_a_wrong_code_never_settles_the_cash(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _settings(db_session, delivery_proof_required=True)
    driver = await _driver(db_session, est)
    order_id, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed")
    await _hold(db_session, order_id)
    good = await proof.get_code(db_session, demo_tenant_slug, order_id)
    wrong = "0000" if good != "0000" else "1111"

    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code=wrong, cash_received=25)) == "DELIVERY_CODE_INVALID"
    assert await db_session.scalar(select(Payment).where(Payment.order_id == order_id, Payment.provider == "cash")) is None
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.payment_status == "guaranteed"

    await _deliver(db_session, demo_tenant_slug, driver, delivery_id, code=good, cash_received=25)
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.status == "delivered" and order.payment_status == "paid"


async def test_with_proof_off_the_code_is_optional_but_checked_when_given(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver)

    assert await _code(_deliver(db_session, demo_tenant_slug, driver, delivery_id, code="0000" if await proof.get_code(db_session, demo_tenant_slug, order_id) != "0000" else "1111")) == "DELIVERY_CODE_INVALID"
    assert (await _deliver(db_session, demo_tenant_slug, driver, delivery_id)).status == "delivered"


async def test_staff_cannot_bypass_the_code_through_the_generic_status_route(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _settings(db_session, delivery_proof_required=True)
    driver = await _driver(db_session, est)
    order_id, _ = await _en_route(db_session, demo_tenant_slug, est, driver)

    assert await _code(orders_service.update_status(db_session, order_id, "delivered", tenant_slug=demo_tenant_slug)) == "DELIVERY_PROOF_REQUIRED"
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.status == "out_for_delivery"

    # Une livraison sans livreur (comportement historique) n'est pas concernee.
    legacy = await _order(db_session, est, status="out_for_delivery")
    legacy_id = legacy.id
    assert (await orders_service.update_status(db_session, legacy_id, "delivered", tenant_slug=demo_tenant_slug)).status == "delivered"


async def test_admin_override_needs_a_reason_is_journaled_and_respects_payment(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _settings(db_session, delivery_proof_required=True)
    driver = await _driver(db_session, est)
    order_id, _ = await _en_route(db_session, demo_tenant_slug, est, driver)
    unpaid_id, _ = await _en_route(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed")

    kwargs = {"admin_user_id": 9, "tenant_slug": demo_tenant_slug}
    assert await _code(svc.admin_deliver_without_code(db_session, order_id=order_id, reason=" ", **kwargs)) == "OVERRIDE_REASON_REQUIRED"
    assert await _code(svc.admin_deliver_without_code(db_session, order_id=unpaid_id, reason="Client absent du code", **kwargs)) == "PAYMENT_SETTLEMENT_REQUIRED"
    refused = await db_session.scalar(select(DeliveryEvent).where(DeliveryEvent.order_id == unpaid_id, DeliveryEvent.event == "delivered_without_code"))
    assert refused is None  # le refus n'a rien journalise

    done = await svc.admin_deliver_without_code(db_session, order_id=order_id, reason="Code perdu, client identifie", **kwargs)
    assert done.status == "delivered"
    events = {e.event: e for e in (await db_session.execute(select(DeliveryEvent).where(DeliveryEvent.order_id == order_id))).scalars()}
    assert events["delivered_without_code"].actor_user_id == 9
    assert events["delivered_without_code"].note == "Code perdu, client identifie"
    assert events["delivered"].note == "proof:admin_override"


async def test_departure_gives_the_code_to_the_client_by_push_or_sms(db_session, demo_tenant_slug, monkeypatch):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    app_order = await _order(db_session, est, user_id=5)
    counter_order = await _order(db_session, est, user_id=None)
    ids = (app_order.id, counter_order.id)
    for order_id in ids:
        await svc.assign(db_session, order_ids=[order_id], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)
    pushed, sms = AsyncMock(), AsyncMock()
    monkeypatch.setattr(orders_service, "notify_user", pushed)
    monkeypatch.setattr(orders_service, "notify_staff", AsyncMock())
    monkeypatch.setattr("app.core.sms.service.enqueue_sms", sms)

    for order_id in ids:
        await orders_service.update_status(db_session, order_id, "out_for_delivery", tenant_slug=demo_tenant_slug, arq_pool=object())

    app_code = await proof.get_code(db_session, demo_tenant_slug, ids[0])
    counter_code = await proof.get_code(db_session, demo_tenant_slug, ids[1])
    assert app_code in pushed.await_args.kwargs["body"]
    sms.assert_awaited_once()
    assert counter_code in sms.await_args.kwargs["body"]


# --------------------------------------------------------------------------- echecs


async def _fail(session, slug, driver, delivery_id, **kwargs):
    return await failures.report_failure(
        session,
        await _profile(session, driver),
        delivery_id,
        reason=kwargs.pop("reason", "customer_absent"),
        note=kwargs.pop("note", None),
        call_attempts=kwargs.pop("call_attempts", 1),
        user_id=driver["user_id"],
        tenant_slug=slug,
    )


async def test_absent_customer_needs_arrival_waiting_time_and_calls(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    not_arrived_order, not_arrived = await _en_route(db_session, demo_tenant_slug, est, driver, arrived=False)
    _, arrived = await _en_route(db_session, demo_tenant_slug, est, driver)

    assert await _code(_fail(db_session, demo_tenant_slug, driver, not_arrived)) == "FAILURE_ARRIVAL_REQUIRED"
    assert await _code(_fail(db_session, demo_tenant_slug, driver, arrived)) == "FAILURE_TOO_EARLY"  # 5 min par defaut

    await _settings(db_session, failure_min_wait_minutes=0, failure_min_call_attempts=2)
    assert await _code(_fail(db_session, demo_tenant_slug, driver, arrived, call_attempts=1)) == "FAILURE_CALL_REQUIRED"
    order = await db_session.get(Order, not_arrived_order, populate_existing=True)
    assert order.status == "out_for_delivery"  # aucun refus n'a bouge la commande

    failure = await _fail(db_session, demo_tenant_slug, driver, arrived, call_attempts=2)
    assert failure.status == "pending" and failure.fault == "customer" and failure.waited_seconds is not None
    delivery = await db_session.get(Delivery, arrived, populate_existing=True)
    assert delivery.status == "failed"
    order = await db_session.get(Order, failure.order_id, populate_existing=True)
    assert order.status == "delivery_failed"


async def test_waiting_time_is_enforced_from_the_arrival_instant(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    _, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver)
    delivery = await db_session.get(Delivery, delivery_id)
    delivery.arrived_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    await db_session.commit()

    failure = await _fail(db_session, demo_tenant_slug, driver, delivery_id, reason="customer_unreachable")
    assert failure.waited_seconds >= 6 * 60


async def test_other_failure_reasons_need_no_wait_but_are_validated(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    _, wrong_address = await _en_route(db_session, demo_tenant_slug, est, driver, arrived=False)
    _, problem = await _en_route(db_session, demo_tenant_slug, est, driver, arrived=False)
    _, other = await _en_route(db_session, demo_tenant_slug, est, driver, arrived=False)

    assert await _code(_fail(db_session, demo_tenant_slug, driver, other, reason="inconnu")) == "FAILURE_REASON_INVALID"
    assert await _code(_fail(db_session, demo_tenant_slug, driver, other, reason="other", note=" ")) == "FAILURE_NOTE_REQUIRED"
    wrong = await _fail(db_session, demo_tenant_slug, driver, wrong_address, reason="wrong_address", call_attempts=0)
    assert wrong.fault == "customer"
    fault = await _fail(db_session, demo_tenant_slug, driver, problem, reason="order_problem", note="Pizza renversee")
    assert fault.fault == "restaurant" and fault.note == "Pizza renversee"
    # Une livraison deja close ne se declare pas deux fois.
    assert await _code(_fail(db_session, demo_tenant_slug, driver, problem, reason="order_problem", note="Encore")) == "DELIVERY_NOT_EN_ROUTE"


async def test_another_driver_cannot_declare_a_failure(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    owner = await _driver(db_session, est)
    thief = await _driver(db_session, est)
    _, delivery_id = await _en_route(db_session, demo_tenant_slug, est, owner, arrived=False)

    assert await _code(_fail(db_session, demo_tenant_slug, thief, delivery_id, reason="wrong_address")) == "DELIVERY_NOT_FOUND"


async def test_failures_list_shows_pending_with_context(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, delivery_id = await _en_route(db_session, demo_tenant_slug, est, driver, arrived=False)
    await _fail(db_session, demo_tenant_slug, driver, delivery_id, reason="wrong_address", call_attempts=0)

    pending = [f for f in await failures.list_failures(db_session, "pending") if f["order_id"] == order_id]
    assert len(pending) == 1
    item = pending[0]
    assert item["reason_label"] == "Adresse introuvable ou fausse" and item["driver_name"] == "Marc Livreur"
    assert item["total"] == 25.0 and item["customer_phone"] == "0698765432"
    assert [f for f in await failures.list_failures(db_session, "resolved") if f["order_id"] == order_id] == []


# --------------------------------------------------------------------------- traitement admin


async def _failed_order(session, slug, est, driver, *, payment_status, reason="wrong_address", total=25.0):
    order_id, delivery_id = await _en_route(session, slug, est, driver, arrived=False, payment_status=payment_status, total=total)
    if payment_status == "guaranteed":
        await _hold(session, order_id, total)
    else:
        session.add(Payment(order_id=order_id, provider="cash", amount=total, currency="EUR", status="paid"))
        await session.commit()
    failure = await _fail(session, slug, driver, delivery_id, reason=reason, note="Sonnette HS" if reason == "other" else None, call_attempts=0)
    return order_id, failure.id


async def _resolve(session, slug, failure_id, **kwargs):
    return await failures.resolve_failure(
        session,
        failure_id,
        action=kwargs.pop("action"),
        amount_cents=kwargs.pop("amount_cents", None),
        note=kwargs.pop("note", None),
        admin_user_id=1,
        tenant_slug=slug,
    )


async def test_refund_releases_a_card_hold_and_cannot_be_applied_twice(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed")

    resolved = await _resolve(db_session, demo_tenant_slug, failure_id, action="refund", note="Geste commercial")
    assert resolved.status == "resolved" and resolved.resolution == "refund" and resolved.resolved_by_user_id == 1
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.payment_status == "guarantee_released"
    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="refund")) == "FAILURE_ALREADY_RESOLVED"


async def test_refund_of_a_paid_order_refunds_the_payment(db_session, demo_tenant_slug):
    from app.modules.payments.models import Refund

    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="paid")

    await _resolve(db_session, demo_tenant_slug, failure_id, action="refund")
    refunds = (await db_session.execute(select(Refund).where(Refund.order_id == order_id))).scalars().all()
    assert [r.amount for r in refunds] == [2500]  # centimes


async def test_retain_keeps_part_of_a_card_hold_for_customer_fault(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed")

    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="retain", amount_cents=500)) == "RESOLUTION_NOTE_REQUIRED"
    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="retain", amount_cents=9999, note="Frais")) == "RETAIN_AMOUNT_INVALID"
    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="retain", note="Frais")) == "RETAIN_AMOUNT_INVALID"

    resolved = await _resolve(db_session, demo_tenant_slug, failure_id, action="retain", amount_cents=500, note="Client absent apres 3 appels")
    assert resolved.resolution == "retain" and float(resolved.retained_amount) == 5.0
    hold = await db_session.scalar(select(Payment).where(Payment.order_id == order_id, Payment.purpose == "guarantee"))
    assert hold.status == "paid" and float(hold.captured_amount) == 5.0
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.payment_status == "guarantee_captured"


async def test_retain_on_a_paid_order_refunds_the_rest(db_session, demo_tenant_slug):
    from app.modules.payments.models import Refund

    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="paid")

    resolved = await _resolve(db_session, demo_tenant_slug, failure_id, action="retain", amount_cents=500, note="Frais de course")
    assert float(resolved.retained_amount) == 5.0
    refunds = (await db_session.execute(select(Refund).where(Refund.order_id == order_id))).scalars().all()
    assert [r.amount for r in refunds] == [2000]  # on rend 20, on garde 5


async def test_nothing_can_be_retained_for_a_restaurant_fault(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed", reason="order_problem")

    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="retain", amount_cents=500, note="Frais")) == "RETAIN_NOT_ALLOWED"
    hold = await db_session.scalar(select(Payment).where(Payment.order_id == order_id, Payment.purpose == "guarantee"))
    assert hold.status == "authorized"  # rien n'a ete debite
    assert (await _resolve(db_session, demo_tenant_slug, failure_id, action="refund")).resolution == "refund"


async def test_redelivery_puts_the_order_back_on_the_board_with_a_new_code(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="paid")
    await proof.ensure_nonce(db_session, order_id)
    old_nonce = await db_session.scalar(select(Order.delivery_code_nonce).where(Order.id == order_id))

    resolved = await _resolve(db_session, demo_tenant_slug, failure_id, action="redeliver", note="Client rappele")
    assert resolved.resolution == "redeliver"
    order = await db_session.get(Order, order_id, populate_existing=True)
    assert order.status == "ready"
    assert order.delivery_code_nonce != old_nonce  # l'ancien code ne vaut plus rien
    board = await svc.dispatch_board(db_session, est.id)
    assert order_id in [o["order_id"] for o in board["unassigned"]]
    # ... et peut etre attribuee a nouveau.
    (delivery,) = await svc.assign(db_session, order_ids=[order_id], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)
    assert delivery.status == "assigned"


async def test_redelivery_is_refused_when_the_order_is_no_longer_paid(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, failure_id = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="guaranteed")
    order = await db_session.get(Order, order_id)
    order.payment_status = "guarantee_released"
    await db_session.commit()

    assert await _code(_resolve(db_session, demo_tenant_slug, failure_id, action="redeliver")) == "REDELIVERY_NOT_POSSIBLE"
    failure = await db_session.get(DeliveryFailure, failure_id, populate_existing=True)
    assert failure.status == "pending"


async def test_generic_status_route_cannot_reopen_a_failed_order(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order_id, _ = await _failed_order(db_session, demo_tenant_slug, est, driver, payment_status="paid")

    assert await _code(orders_service.update_status(db_session, order_id, "ready", tenant_slug=demo_tenant_slug)) == "INVALID_STATUS_TRANSITION"


# --------------------------------------------------------------------------- reglages


async def test_proof_and_failure_settings_are_validated_and_audited(db_session):
    from app.modules.delivery.models import RestaurantDeliverySettingsAudit
    from app.modules.delivery.schemas import DeliverySettingsUpdate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeliverySettingsUpdate(internal_enabled=True, expected_version=1, failure_min_wait_minutes=999)
    with pytest.raises(ValidationError):
        DeliverySettingsUpdate(internal_enabled=True, expected_version=1, failure_min_call_attempts=-1)

    row = await delivery_service.get_delivery_settings(db_session)
    await db_session.commit()
    version = row.version
    updated = await delivery_service.update_delivery_settings(
        db_session,
        internal_enabled=True,
        delivery_proof_required=True,
        failure_min_wait_minutes=10,
        expected_version=version,
        user_id=1,
        user_email="a@b.fr",
        ip_address="127.0.0.1",
        user_agent="t",
    )
    assert updated.delivery_proof_required is True and updated.failure_min_wait_minutes == 10
    assert updated.failure_min_call_attempts == 1  # non mentionne : inchange
    fields = {a.field_name for a in (await db_session.execute(select(RestaurantDeliverySettingsAudit))).scalars()}
    assert {"delivery_proof_required", "failure_min_wait_minutes"} <= fields


# --------------------------------------------------------------------------- isolation HTTP


async def test_failure_routes_are_admin_only_and_closed_to_drivers(client, bootstrap_default_tenant):
    slug = bootstrap_default_tenant["tenant_slug"]

    def token(role, user_id, email, permissions):
        return create_access_token(
            {
                "sub": str(user_id),
                "email": email,
                "role": role,
                "tenant_id": bootstrap_default_tenant["tenant_id"],
                "tenant_slug": slug,
                "permissions": permissions,
                "must_change_password": False,
            }
        )

    staff = {"Authorization": "Bearer " + token("staff", bootstrap_default_tenant["staff_user_id"], "staff@test.com", ["*"])}
    admin = {"Authorization": "Bearer " + token("admin", bootstrap_default_tenant["user_id"], "admin@test.com", None)}

    # Le personnel ne traite pas les echecs et ne livre pas sans code ; l'admin les voit.
    assert (await client.get("/api/v1/delivery/failures", headers=admin)).status_code == 200
    resolve = await client.post("/api/v1/delivery/failures/1/resolve", headers=staff, json={"action": "refund"})
    assert resolve.status_code == 403
    override = await client.post(
        "/api/v1/delivery/dispatch/orders/1/deliver-without-code", headers=staff, json={"reason": "test test"}
    )
    assert override.status_code == 403
    # Le code de remise d'une commande n'est lisible que par son client.
    assert (await client.get("/api/v1/orders/1/delivery-code")).status_code == 401
