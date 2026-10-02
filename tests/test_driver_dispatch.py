"""Phase 3 livraison interne : livreurs, dispatch comptoir, ecran livreur.

Base reelle (savepoint rollbackee par test). Les helpers committent apres leur preparation : les
fonctions testees font des ``rollback()`` pour liberer leurs verrous, ce qui annulerait sinon la
preparation non committee.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.auth.security import create_access_token
from app.core.database import get_tenant_session
from app.core.http.errors import AppError
from app.modules.auth.models import User
from app.modules.delivery import dispatch_service as svc
from app.modules.delivery import service as delivery_service
from app.modules.delivery.models import Delivery, DeliveryEvent, DeliveryRun, DriverProfile
from app.modules.hr.models import EmployeeProfile, Establishment, TimeClockEntry
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments.models import Payment


async def _establishment(session, name="Resto A"):
    est = Establishment(name=name, timezone="Europe/Paris", is_active=True)
    session.add(est)
    await session.commit()
    return est


async def _clock_in(session, user_id, est_id, status="open"):
    emp = await session.scalar(select(EmployeeProfile).where(EmployeeProfile.user_id == user_id))
    session.add(
        TimeClockEntry(
            employee_id=emp.id,
            establishment_id=est_id,
            clock_in_at=datetime.now(timezone.utc),
            method="web",
            status=status,
        )
    )
    await session.commit()


async def _driver(session, est, *, clocked=True, name="Marc Livreur"):
    out = await svc.create_driver(
        session,
        email=f"driver-{uuid.uuid4().hex[:8]}@test.fr",
        full_name=name,
        phone="0612345678",
        vehicle="Scooter",
        establishment_id=est.id,
    )
    if clocked:
        await _clock_in(session, out["user_id"], est.id)
    return out


async def _order(session, est, *, status="ready", payment_status="paid", order_type="delivery", total=25.0):
    order = Order(
        user_id=None,
        establishment_id=est.id,
        status=status,
        payment_status=payment_status,
        order_type=order_type,
        total=total,
        subtotal=total,
        customer_name="Claire D.",
        customer_phone="0698765432",
        delivery_address="1 rue de la Paix, Paris" if order_type == "delivery" else None,
        delivery_lat=48.87,
        delivery_lng=2.33,
    )
    session.add(order)
    await session.commit()
    return order


async def _enable_dispatch(session):
    row = await delivery_service.get_delivery_settings(session)
    row.driver_dispatch_enabled = True
    await session.commit()


async def _assign(session, slug, orders, driver, actor=1):
    return await svc.assign(
        session,
        order_ids=[o.id for o in orders],
        driver_id=driver["id"],
        actor_user_id=actor,
        tenant_slug=slug,
    )


async def _driver_call(fn, session, profile, *args, **kwargs):
    """Les fonctions livreur font un rollback (liberation des verrous) qui expire le profil."""
    await session.refresh(profile)
    return await fn(session, profile, *args, **kwargs)


async def _code(awaitable) -> str:
    with pytest.raises(AppError) as err:
        await awaitable
    return err.value.code


# --------------------------------------------------------------------------- creation


async def test_create_driver_makes_account_employee_and_profile(db_session):
    est = await _establishment(db_session)
    out = await _driver(db_session, est, clocked=False)

    user = await db_session.get(User, out["user_id"])
    assert user.role == "driver" and user.must_change_password is True
    assert user.permissions == []
    assert user.password_hash != out["temporary_password"]  # jamais en clair
    assert await db_session.scalar(select(EmployeeProfile).where(EmployeeProfile.user_id == user.id))
    assert (await db_session.get(DriverProfile, out["id"])).establishment_id == est.id
    assert out["clocked_in"] is False and out["is_active"] is True


async def test_create_driver_refuses_duplicate_email_and_unknown_establishment(db_session):
    est = await _establishment(db_session)
    first = await _driver(db_session, est, clocked=False)
    assert (
        await _code(
            svc.create_driver(
                db_session, email=first["email"].upper(), full_name="X", phone=None, vehicle=None,
                establishment_id=est.id,
            )
        )
        == "EMAIL_EXISTS"
    )
    assert (
        await _code(
            svc.create_driver(
                db_session, email="new@test.fr", full_name="X", phone=None, vehicle=None, establishment_id=99999
            )
        )
        == "ESTABLISHMENT_NOT_FOUND"
    )


# --------------------------------------------------------------------------- attribution


async def test_assign_creates_delivery_and_event_then_lists_on_board(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, status="preparing")

    (delivery,) = await _assign(db_session, demo_tenant_slug, [order], driver, actor=42)

    assert delivery.status == "assigned" and delivery.assigned_by_user_id == 42
    events = (await db_session.execute(select(DeliveryEvent).where(DeliveryEvent.order_id == order.id))).scalars().all()
    assert [e.event for e in events] == ["assigned"] and events[0].actor_user_id == 42

    board = await svc.dispatch_board(db_session, est.id)
    assert board["unassigned"] == []
    assert [d["order"]["order_id"] for d in board["deliveries"]] == [order.id]
    assert board["deliveries"][0]["driver_name"] == "Marc Livreur"
    assert board["drivers"][0]["active_deliveries"] == 1 and board["drivers"][0]["clocked_in"] is True


async def test_board_lists_assignable_delivery_orders_only(db_session):
    est = await _establishment(db_session)
    ok = await _order(db_session, est, status="confirmed")
    await _order(db_session, est, status="pending", payment_status="pending")
    await _order(db_session, est, status="confirmed", order_type="pickup")
    await _order(db_session, est, status="out_for_delivery")
    await _order(db_session, est, status="delivered")

    board = await svc.dispatch_board(db_session, est.id)
    assert [o["order_id"] for o in board["unassigned"]] == [ok.id]
    assert board["unassigned"][0]["customer_phone"] == "0698765432"


async def test_assign_multiple_orders_to_one_driver(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    orders = [await _order(db_session, est) for _ in range(3)]

    result = await _assign(db_session, demo_tenant_slug, orders, driver)
    assert sorted(d.order_id for d in result) == sorted(o.id for o in orders)
    assert {d.driver_id for d in result} == {driver["id"]}


async def test_assign_refusals(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    other_est = await _establishment(db_session, "Resto B")
    driver = await _driver(db_session, est)
    absent = await _driver(db_session, est, clocked=False)
    ready = await _order(db_session, est)

    assert await _code(_assign(db_session, demo_tenant_slug, [ready], absent)) == "DRIVER_NOT_CLOCKED_IN"
    assert await _code(_assign(db_session, demo_tenant_slug, [await _order(db_session, est, status="pending", payment_status="pending")], driver)) == "ORDER_NOT_ASSIGNABLE"
    assert await _code(_assign(db_session, demo_tenant_slug, [await _order(db_session, est, order_type="pickup")], driver)) == "ORDER_NOT_DELIVERY"
    assert await _code(_assign(db_session, demo_tenant_slug, [await _order(db_session, other_est)], driver)) == "DRIVER_WRONG_ESTABLISHMENT"
    assert await _code(svc.assign(db_session, order_ids=[ready.id], driver_id=99999, actor_user_id=1, tenant_slug=demo_tenant_slug)) == "DRIVER_NOT_FOUND"
    assert await _code(svc.assign(db_session, order_ids=[987654], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)) == "ORDER_NOT_FOUND"

    await svc.update_driver(db_session, absent["id"], {"is_active": False})
    assert await _code(_assign(db_session, demo_tenant_slug, [ready], absent)) == "DRIVER_INACTIVE"


async def test_assign_is_all_or_nothing(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    good = await _order(db_session, est)
    bad = await _order(db_session, est, status="delivered")

    good_id = good.id
    assert await _code(_assign(db_session, demo_tenant_slug, [good, bad], driver)) == "ORDER_NOT_ASSIGNABLE"
    await db_session.rollback()
    assert await db_session.scalar(select(Delivery).where(Delivery.order_id == good_id)) is None


async def test_reassign_unassign_and_started_deliveries_are_protected(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    a = await _driver(db_session, est, name="A")
    b = await _driver(db_session, est, name="B")
    order = await _order(db_session, est)

    (first,) = await _assign(db_session, demo_tenant_slug, [order], a)
    (again,) = await _assign(db_session, demo_tenant_slug, [order], a)  # idempotent
    assert again.id == first.id
    (moved,) = await _assign(db_session, demo_tenant_slug, [order], b)
    assert moved.id == first.id and moved.driver_id == b["id"]
    events = [e.event for e in (await db_session.execute(select(DeliveryEvent).where(DeliveryEvent.order_id == order.id).order_by(DeliveryEvent.id))).scalars()]
    assert events == ["assigned", "reassigned"]

    removed = await svc.unassign(db_session, order_id=order.id, actor_user_id=1)
    assert removed.status == "cancelled"
    assert (await svc.dispatch_board(db_session, est.id))["unassigned"][0]["order_id"] == order.id

    # Une fois partie, la commande ne se reattribue ni ne se retire plus.
    await _assign(db_session, demo_tenant_slug, [order], a)
    await orders_service.update_status(db_session, order.id, "out_for_delivery", tenant_slug=demo_tenant_slug)
    assert await _code(_assign(db_session, demo_tenant_slug, [order], b)) == "DELIVERY_ALREADY_STARTED"
    assert await _code(svc.unassign(db_session, order_id=order.id, actor_user_id=1)) == "DELIVERY_ALREADY_STARTED"


async def test_deactivating_a_driver_with_active_deliveries_is_refused(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [order], driver)

    assert await _code(svc.update_driver(db_session, driver["id"], {"is_active": False})) == "DRIVER_HAS_ACTIVE_DELIVERIES"
    await svc.unassign(db_session, order_id=order.id, actor_user_id=1)
    assert (await svc.update_driver(db_session, driver["id"], {"is_active": False}))["is_active"] is False


# --------------------------------------------------------------------------- depart : drapeau et synchronisation


async def test_without_dispatch_flag_departure_works_as_before(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    order = await _order(db_session, est)

    updated = await orders_service.update_status(db_session, order.id, "out_for_delivery", tenant_slug=demo_tenant_slug)
    assert updated.status == "out_for_delivery"


async def test_with_dispatch_flag_departure_requires_an_assigned_driver(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _enable_dispatch(db_session)
    order = await _order(db_session, est)

    code = await _code(orders_service.update_status(db_session, order.id, "out_for_delivery", tenant_slug=demo_tenant_slug))
    assert code == "DRIVER_REQUIRED"
    await db_session.refresh(order)
    assert order.status == "ready"  # rien n'a bouge

    # Les commandes a emporter ne sont pas concernees.
    pickup = await _order(db_session, est, order_type="pickup")
    assert (await orders_service.update_status(db_session, pickup.id, "delivered", tenant_slug=demo_tenant_slug)).status == "delivered"


async def test_delivery_follows_the_order_and_closes_the_run(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _enable_dispatch(db_session)
    driver = await _driver(db_session, est)
    first, second = await _order(db_session, est), await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [first, second], driver)

    await orders_service.update_status(db_session, first.id, "out_for_delivery", tenant_slug=demo_tenant_slug)
    await orders_service.update_status(db_session, second.id, "out_for_delivery", tenant_slug=demo_tenant_slug)
    d1 = await db_session.scalar(select(Delivery).where(Delivery.order_id == first.id))
    d2 = await db_session.scalar(select(Delivery).where(Delivery.order_id == second.id))
    assert d1.status == d2.status == "out_for_delivery" and d1.departed_at is not None
    assert d1.run_id == d2.run_id  # un seul depart = une tournee

    await orders_service.update_status(db_session, first.id, "delivered", tenant_slug=demo_tenant_slug)
    run = await db_session.get(DeliveryRun, d1.run_id)
    assert run.status == "active"  # il reste une livraison
    await orders_service.update_status(db_session, second.id, "delivered", tenant_slug=demo_tenant_slug)
    await db_session.refresh(run)
    assert run.status == "completed" and run.ended_at is not None


@pytest.mark.parametrize(
    "closing_status, expected",
    [("cancelled", "cancelled"), ("delivery_failed", "failed")],
)
async def test_cancel_or_failure_closes_the_active_delivery(db_session, demo_tenant_slug, closing_status, expected):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, status="preparing" if closing_status == "cancelled" else "ready")
    await _assign(db_session, demo_tenant_slug, [order], driver)
    if closing_status == "delivery_failed":
        await orders_service.update_status(db_session, order.id, "out_for_delivery", tenant_slug=demo_tenant_slug)

    await orders_service.update_status(
        db_session, order.id, closing_status, "Client absent", tenant_slug=demo_tenant_slug
    )
    delivery = await db_session.scalar(select(Delivery).where(Delivery.order_id == order.id))
    assert delivery.status == expected and delivery.finished_at is not None


async def test_one_active_delivery_per_order_is_enforced_by_the_database(db_session, demo_tenant_slug):
    from sqlalchemy.exc import IntegrityError

    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [order], driver)

    db_session.add(Delivery(order_id=order.id, driver_id=driver["id"], status="assigned"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --------------------------------------------------------------------------- notifications client


async def test_client_gets_delivery_specific_messages(db_session, demo_tenant_slug, monkeypatch):
    from unittest.mock import AsyncMock

    est = await _establishment(db_session)
    sent = AsyncMock()
    monkeypatch.setattr(orders_service, "notify_user", sent)
    monkeypatch.setattr(orders_service, "notify_staff", AsyncMock())
    delivery = await _order(db_session, est, status="preparing")
    pickup = await _order(db_session, est, status="preparing", order_type="pickup")
    for order in (delivery, pickup):
        order.user_id = 5
    await db_session.commit()

    await orders_service.update_status(db_session, delivery.id, "ready", tenant_slug=demo_tenant_slug)
    await orders_service.update_status(db_session, pickup.id, "ready", tenant_slug=demo_tenant_slug)
    await orders_service.update_status(db_session, delivery.id, "out_for_delivery", tenant_slug=demo_tenant_slug)

    calls = [(c.kwargs["event"], c.kwargs["title"], c.kwargs["body"]) for c in sent.await_args_list]
    assert calls[0][:2] == ("order.ready", "Commande prête")  # livraison : plus de « a recuperer »
    assert "livreur" in calls[0][2]
    assert calls[1][:2] == ("order.ready", "Prête à récupérer")  # retrait inchange
    assert calls[2][:2] == ("order.out_for_delivery", "Votre livreur est en route")
    assert f"#{delivery.id}" in calls[2][2]


# --------------------------------------------------------------------------- cote livreur


async def test_driver_sees_only_own_deliveries_with_phases(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    me = await _driver(db_session, est, name="Moi")
    other = await _driver(db_session, est, name="Autre")
    ready = await _order(db_session, est, status="ready", payment_status="guaranteed", total=30)
    cooking = await _order(db_session, est, status="preparing")
    theirs = await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [ready, cooking], me)
    await _assign(db_session, demo_tenant_slug, [theirs], other)

    profile = await db_session.get(DriverProfile, me["id"])
    deliveries = await svc.driver_deliveries(db_session, profile)

    by_order = {d["order_id"]: d for d in deliveries}
    assert set(by_order) == {ready.id, cooking.id}
    assert by_order[ready.id]["phase"] == "ready" and by_order[cooking.id]["phase"] == "upcoming"
    assert by_order[ready.id]["amount_due"] == 30.0 and by_order[cooking.id]["amount_due"] == 0.0
    assert by_order[ready.id]["customer_phone"] == "0698765432"
    assert "customer_email" not in by_order[ready.id]  # le livreur n'a que ce qu'il lui faut


async def test_driver_departs_only_ready_own_orders(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    me = await _driver(db_session, est, name="Moi")
    other = await _driver(db_session, est, name="Autre")
    ready = await _order(db_session, est)
    cooking = await _order(db_session, est, status="preparing")
    theirs = await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [ready, cooking], me)
    await _assign(db_session, demo_tenant_slug, [theirs], other)
    profile = await db_session.get(DriverProfile, me["id"])
    ids = {
        o.id: (await db_session.scalar(select(Delivery.id).where(Delivery.order_id == o.id)))
        for o in (ready, cooking, theirs)
    }

    kwargs = {"user_id": me["user_id"], "tenant_slug": demo_tenant_slug}
    assert await _code(_driver_call(svc.driver_depart, db_session, profile, [ids[cooking.id]], **kwargs)) == "ORDER_NOT_READY"
    assert await _code(_driver_call(svc.driver_depart, db_session, profile, [ids[theirs.id]], **kwargs)) == "DELIVERY_NOT_FOUND"
    assert await _code(_driver_call(svc.driver_depart, db_session, profile, [ids[ready.id], ids[cooking.id]], **kwargs)) == "ORDER_NOT_READY"
    await db_session.refresh(ready)
    assert ready.status == "ready"  # refus d'un lot : aucune commande n'est partie

    (departed,) = await _driver_call(svc.driver_depart, db_session, profile, [ids[ready.id]], **kwargs)
    assert departed.status == "out_for_delivery" and departed.run_id is not None
    await db_session.refresh(ready)
    assert ready.status == "out_for_delivery"


async def test_driver_full_flow_with_cash_on_delivery(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, payment_status="guaranteed", total=25)
    db_session.add(
        Payment(
            order_id=order.id, provider="stripe", provider_payment_id="local_hold", purpose="guarantee",
            amount=25, currency="EUR", status="authorized",
        )
    )
    await db_session.commit()
    await _assign(db_session, demo_tenant_slug, [order], driver)
    profile = await db_session.get(DriverProfile, driver["id"])
    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order.id))
    kwargs = {"user_id": driver["user_id"], "tenant_slug": demo_tenant_slug}

    # Livrer avant d'etre parti : refuse.
    assert await _code(_driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=25, **kwargs)) == "DELIVERY_NOT_EN_ROUTE"
    await _driver_call(svc.driver_depart, db_session, profile, [delivery_id], **kwargs)
    arrived = await _driver_call(svc.driver_arrived, db_session, profile, delivery_id, **kwargs)
    assert arrived.status == "arrived" and arrived.arrived_at is not None
    assert (await _driver_call(svc.driver_arrived, db_session, profile, delivery_id, **kwargs)).status == "arrived"  # idempotent

    # Empreinte a regler : les especes sont obligatoires et suffisantes.
    assert await _code(_driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=None, **kwargs)) == "CASH_RECEIVED_REQUIRED"
    assert await _code(_driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=10, **kwargs)) == "CASH_AMOUNT_INSUFFICIENT"
    await db_session.refresh(order)
    assert order.status == "out_for_delivery"

    done = await _driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=30, **kwargs)
    assert done.status == "delivered" and done.finished_at is not None
    await db_session.refresh(order)
    assert order.status == "delivered" and order.payment_status == "paid"
    cash = await db_session.scalar(select(Payment).where(Payment.order_id == order.id, Payment.provider == "cash"))
    assert cash.settled_by_user_id == driver["user_id"] and float(cash.amount_received) == 30.0

    recap = await _driver_call(svc.driver_recap, db_session, profile)
    assert recap["delivered_count"] == 1 and recap["runs_count"] == 1
    assert recap["cash_collected"] == 25.0
    assert recap["deliveries"][0]["order_id"] == order.id


async def test_driver_delivers_an_online_paid_order_without_cash(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, payment_status="paid")
    await _assign(db_session, demo_tenant_slug, [order], driver)
    profile = await db_session.get(DriverProfile, driver["id"])
    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order.id))
    kwargs = {"user_id": driver["user_id"], "tenant_slug": demo_tenant_slug}

    await _driver_call(svc.driver_depart, db_session, profile, [delivery_id], **kwargs)
    done = await _driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=None, **kwargs)
    assert done.status == "delivered"
    assert (await _driver_call(svc.driver_recap, db_session, profile))["cash_collected"] == 0.0


async def test_driver_cannot_conclude_an_order_whose_hold_was_released(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, payment_status="guarantee_released")
    await _assign(db_session, demo_tenant_slug, [order], driver)
    profile = await db_session.get(DriverProfile, driver["id"])
    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order.id))
    kwargs = {"user_id": driver["user_id"], "tenant_slug": demo_tenant_slug}
    await _driver_call(svc.driver_depart, db_session, profile, [delivery_id], **kwargs)

    assert await _code(_driver_call(svc.driver_deliver, db_session, profile, delivery_id, cash_received=25, **kwargs)) == "ORDER_NOT_PAID"
    await db_session.refresh(order)
    assert order.status == "out_for_delivery"


async def test_another_driver_cannot_act_on_a_delivery(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    owner = await _driver(db_session, est, name="Proprietaire")
    thief = await _driver(db_session, est, name="Intrus")
    order = await _order(db_session, est)
    await _assign(db_session, demo_tenant_slug, [order], owner)
    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order.id))
    intruder = await db_session.get(DriverProfile, thief["id"])
    kwargs = {"user_id": thief["user_id"], "tenant_slug": demo_tenant_slug}

    assert await _code(_driver_call(svc.driver_arrived, db_session, intruder, delivery_id, **kwargs)) == "DELIVERY_NOT_FOUND"
    assert await _code(_driver_call(svc.driver_deliver, db_session, intruder, delivery_id, cash_received=25, **kwargs)) == "DELIVERY_NOT_FOUND"


async def test_inactive_driver_profile_is_locked_out(db_session):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    await svc.update_driver(db_session, driver["id"], {"is_active": False})

    assert await _code(svc.get_driver_for_user(db_session, driver["user_id"])) == "DRIVER_INACTIVE"
    assert await _code(svc.get_driver_for_user(db_session, 987654)) == "DRIVER_PROFILE_REQUIRED"


# --------------------------------------------------------------------------- reglage du dispatch


async def test_dispatch_flag_needs_an_active_driver_and_is_audited(db_session):
    from app.modules.delivery.models import RestaurantDeliverySettingsAudit

    est = await _establishment(db_session)
    est_id = est.id
    row = await delivery_service.get_delivery_settings(db_session)
    await db_session.commit()
    version = row.version
    common = dict(
        internal_enabled=True, user_id=1, user_email="a@b.fr", ip_address="127.0.0.1", user_agent="t"
    )

    code = await _code(
        delivery_service.update_delivery_settings(
            db_session, driver_dispatch_enabled=True, expected_version=version, **common
        )
    )
    assert code == "NO_ACTIVE_DRIVER"
    await db_session.rollback()

    est = await db_session.get(Establishment, est_id)  # expire par le rollback
    await _driver(db_session, est, clocked=False)
    row = await delivery_service.get_delivery_settings(db_session)
    updated = await delivery_service.update_delivery_settings(
        db_session, driver_dispatch_enabled=True, expected_version=row.version, **common
    )
    assert updated.driver_dispatch_enabled is True
    audit = await db_session.scalar(
        select(RestaurantDeliverySettingsAudit).where(
            RestaurantDeliverySettingsAudit.field_name == "driver_dispatch_enabled"
        )
    )
    assert audit.old_value == "false" and audit.new_value == "true"

    # Un appel qui ne mentionne pas le champ (ancienne app) ne le modifie pas.
    kept = await delivery_service.update_delivery_settings(
        db_session, expected_version=updated.version, **common
    )
    assert kept.driver_dispatch_enabled is True


# --------------------------------------------------------------------------- isolation HTTP du role driver


async def test_driver_role_is_confined_to_clock_in_and_deliveries(client, bootstrap_default_tenant):
    slug = bootstrap_default_tenant["tenant_slug"]
    suffix = uuid.uuid4().hex[:8]
    est_id = user_id = None
    try:
        async with get_tenant_session(slug) as session:
            est = Establishment(name=f"HTTP {suffix}", timezone="Europe/Paris", is_active=True)
            session.add(est)
            await session.commit()
            est_id = est.id
            out = await svc.create_driver(
                session, email=f"http-{suffix}@test.fr", full_name="HTTP Driver", phone=None, vehicle=None,
                establishment_id=est_id,
            )
            user_id, email = out["user_id"], out["email"]
        token = create_access_token(
            {
                "sub": str(user_id),
                "email": email,
                "role": "driver",
                "tenant_id": bootstrap_default_tenant["tenant_id"],
                "tenant_slug": slug,
                "permissions": [],
                "must_change_password": False,
            }
        )
        headers = {"Authorization": f"Bearer {token}"}

        allowed = [
            ("GET", "/api/v1/delivery/driver/me"),
            ("GET", "/api/v1/delivery/driver/deliveries"),
            ("GET", "/api/v1/delivery/driver/recap"),
            ("GET", "/api/v1/hr/employees/me"),
            ("GET", "/api/v1/hr/shifts/me"),
        ]
        for method, path in allowed:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 200, (path, response.status_code, response.text)

        forbidden = [
            ("GET", "/api/v1/orders"),
            ("GET", "/api/v1/payments/summary"),
            ("GET", "/api/v1/payments"),
            ("POST", "/api/v1/payments/1/guarantee/cash-collected"),
            ("POST", "/api/v1/payments/1/guarantee/release"),
            ("GET", "/api/v1/delivery/drivers"),
            ("GET", "/api/v1/delivery/dispatch/board"),
            ("POST", "/api/v1/delivery/dispatch/assign"),
            ("GET", "/api/v1/delivery/zones/manage"),
            ("GET", "/api/v1/delivery/settings"),
            ("GET", "/api/v1/hr/employees"),
            ("GET", "/api/v1/admin/users"),
            ("GET", "/api/v1/stock/ingredients"),
        ]
        for method, path in forbidden:
            response = await client.request(method, path, headers=headers, json={} if method == "POST" else None)
            assert response.status_code in (403, 404, 405), (path, response.status_code)
            assert response.status_code != 200, path

        # Et un compte staff ne peut pas se faire passer pour un livreur.
        staff_token = create_access_token(
            {
                "sub": str(bootstrap_default_tenant["staff_user_id"]),
                "email": "staff-check@test.fr",
                "role": "staff",
                "tenant_id": bootstrap_default_tenant["tenant_id"],
                "tenant_slug": slug,
                "permissions": ["*"],
                "must_change_password": False,
            }
        )
        denied = await client.get(
            "/api/v1/delivery/driver/deliveries", headers={"Authorization": f"Bearer {staff_token}"}
        )
        assert denied.status_code in (401, 403)
    finally:
        async with get_tenant_session(slug) as session:
            from sqlalchemy import delete

            if user_id is not None:
                await session.execute(delete(TimeClockEntry).where(TimeClockEntry.establishment_id == est_id))
                await session.execute(delete(DriverProfile).where(DriverProfile.user_id == user_id))
                await session.execute(delete(EmployeeProfile).where(EmployeeProfile.user_id == user_id))
                await session.execute(delete(User).where(User.id == user_id))
            if est_id is not None:
                await session.execute(delete(Establishment).where(Establishment.id == est_id))
            await session.commit()
