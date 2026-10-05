"""Phase 6 livraison interne : reglages par etablissement, auto-attribution, estimation dynamique."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update

from app.core.database import get_tenant_session
from app.core.http.errors import AppError
from app.modules.auth.models import User
from app.modules.delivery import dispatch_service as svc
from app.modules.delivery import dispatch_settings, estimates, failures
from app.modules.delivery import service as delivery_service
from app.modules.delivery.models import (
    Delivery,
    DeliveryEvent,
    DeliveryFailure,
    DeliveryRun,
    DriverProfile,
    EstablishmentDispatchSettings,
    RestaurantDeliverySettings,
    RestaurantDeliverySettingsAudit,
)
from app.modules.hr.models import EmployeeProfile, Establishment, TimeClockEntry
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order

from types import SimpleNamespace  # noqa: E402

import test_delivery_proof_failures as base  # noqa: E402
from test_delivery_proof_failures import _driver, _en_route, _profile, _settings  # noqa: E402


# Les services font rollback pour liberer leurs verrous, ce qui expire les objets ORM : on ne garde que les ids.
async def _establishment(session, *a, **k):
    return SimpleNamespace(id=(await base._establishment(session, *a, **k)).id)


async def _order(session, est, **k):
    return SimpleNamespace(id=(await base._order(session, est, **k)).id)


async def _code(awaitable) -> str:
    with pytest.raises(AppError) as err:
        await awaitable
    return err.value.code


async def _configure(session, est, *, mode="self_assign", cap=3, dispatch=True, **extra):
    """Etablissement en libre-service et dispatch par livreurs actif (tenant)."""
    if dispatch:
        await _settings(session, driver_dispatch_enabled=True)
    await dispatch_settings.update(
        session,
        est.id,
        {"dispatch_mode": mode, "max_active_deliveries": cap, **extra},
        expected_version=0,
        user_id=1,
        user_email="admin@test.fr",
    )


async def _claim(session, driver, *orders):
    return await svc.claim(
        session, await _profile(session, driver), [o.id if hasattr(o, "id") else o for o in orders], user_id=driver["user_id"]
    )


# --------------------------------------------------------------------------- reglages par etablissement


async def test_defaults_without_a_row_inherit_the_tenant(db_session):
    est = await _establishment(db_session)

    effective = await dispatch_settings.get_effective(db_session, est.id)
    assert effective["dispatch_mode"] == "counter" and effective["max_active_deliveries"] == 3
    assert (effective["failure_min_wait_minutes"], effective["failure_min_call_attempts"]) == (5, 1)
    assert effective["failure_rules_overridden"] is False and effective["version"] == 0


async def test_update_creates_the_row_audits_and_versions(db_session):
    est = await _establishment(db_session)

    out = await dispatch_settings.update(
        db_session, est.id, {"dispatch_mode": "self_assign", "max_active_deliveries": 2, "failure_min_wait_minutes": 8},
        expected_version=0, user_id=7, user_email="a@b.fr", ip_address="127.0.0.1",
    )
    assert out["dispatch_mode"] == "self_assign" and out["max_active_deliveries"] == 2
    assert out["failure_min_wait_minutes"] == 8 and out["failure_rules_overridden"] is True
    assert out["failure_min_call_attempts"] == 1  # herite
    assert out["version"] == 1
    fields = {a.field_name for a in (await db_session.execute(select(RestaurantDeliverySettingsAudit))).scalars()}
    assert {f"establishment_{est.id}.dispatch_mode", f"establishment_{est.id}.max_active_deliveries", f"establishment_{est.id}.failure_min_wait_minutes"} <= fields


async def test_concurrent_edits_are_refused_and_noops_do_not_bump_the_version(db_session):
    est = await _establishment(db_session)
    await dispatch_settings.update(db_session, est.id, {"max_active_deliveries": 2}, expected_version=0, user_id=1, user_email=None)

    assert await _code(
        dispatch_settings.update(db_session, est.id, {"max_active_deliveries": 5}, expected_version=0, user_id=1, user_email=None)
    ) == "DISPATCH_SETTINGS_CONFLICT"
    await db_session.rollback()
    same = await dispatch_settings.update(db_session, est.id, {"max_active_deliveries": 2}, expected_version=1, user_id=1, user_email=None)
    assert same["version"] == 1  # rien n'a change


async def test_invalid_values_and_unknown_establishment_are_refused(db_session):
    est = await _establishment(db_session)
    for values, code in [
        ({"dispatch_mode": "chaos"}, "DISPATCH_MODE_INVALID"),
        ({"max_active_deliveries": 0}, "MAX_ACTIVE_INVALID"),
        ({"max_active_deliveries": 11}, "MAX_ACTIVE_INVALID"),
        ({"failure_min_wait_minutes": 61}, "FAILURE_WAIT_INVALID"),
        ({"failure_min_call_attempts": 6}, "FAILURE_CALLS_INVALID"),
    ]:
        assert await _code(dispatch_settings.update(db_session, est.id, values, expected_version=0, user_id=1, user_email=None)) == code
    assert await _code(dispatch_settings.update(db_session, 987654, {}, expected_version=0, user_id=1, user_email=None)) == "ESTABLISHMENT_NOT_FOUND"


async def test_failure_rules_inherit_override_and_can_be_reset(db_session):
    est = await _establishment(db_session)
    await _settings(db_session, failure_min_wait_minutes=7, failure_min_call_attempts=2)

    assert (await failures.get_rules(db_session, est.id))["min_wait_minutes"] == 7  # herite du tenant
    await dispatch_settings.update(db_session, est.id, {"failure_min_wait_minutes": 1}, expected_version=0, user_id=1, user_email=None)
    rules = await failures.get_rules(db_session, est.id)
    assert (rules["min_wait_minutes"], rules["min_call_attempts"]) == (1, 2)  # attente propre, appels herites

    await dispatch_settings.update(db_session, est.id, {"failure_min_wait_minutes": None}, expected_version=1, user_id=1, user_email=None)
    assert (await failures.get_rules(db_session, est.id))["min_wait_minutes"] == 7  # retour au reglage general
    # Les champs obligatoires ne s'effacent pas.
    out = await dispatch_settings.update(db_session, est.id, {"dispatch_mode": None}, expected_version=2, user_id=1, user_email=None)
    assert out["dispatch_mode"] == "counter"


async def test_each_establishment_applies_its_own_failure_rules(db_session, demo_tenant_slug):
    strict, relaxed = await _establishment(db_session), await _establishment(db_session)
    await dispatch_settings.update(db_session, relaxed.id, {"failure_min_wait_minutes": 0, "failure_min_call_attempts": 0}, expected_version=0, user_id=1, user_email=None)
    d_strict, d_relaxed = await _driver(db_session, strict), await _driver(db_session, relaxed)
    _, delivery_strict = await _en_route(db_session, demo_tenant_slug, strict, d_strict)
    _, delivery_relaxed = await _en_route(db_session, demo_tenant_slug, relaxed, d_relaxed)

    async def fail(driver, delivery_id):
        return await failures.report_failure(
            db_session, await _profile(db_session, driver), delivery_id, reason="customer_absent", note=None,
            call_attempts=0, user_id=driver["user_id"], tenant_slug=demo_tenant_slug,
        )

    assert await _code(fail(d_strict, delivery_strict)) == "FAILURE_TOO_EARLY"  # 5 min par defaut
    assert (await fail(d_relaxed, delivery_relaxed)).status == "pending"  # regles propres : aucune attente
    deliveries = await svc.driver_deliveries(db_session, await _profile(db_session, d_strict))
    assert deliveries[0]["failure_min_wait_minutes"] == 5


# --------------------------------------------------------------------------- liste et prise


async def test_nothing_is_claimable_in_counter_mode_or_with_dispatch_off(db_session):
    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    await _order(db_session, est, status="confirmed")

    counter = await svc.claimable_orders(db_session, await _profile(db_session, driver))
    assert counter["enabled"] is False and counter["orders"] == [] and counter["dispatch_mode"] == "counter"

    await _configure(db_session, est, dispatch=False)
    no_dispatch = await svc.claimable_orders(db_session, await _profile(db_session, driver))
    assert no_dispatch["enabled"] is False and no_dispatch["orders"] == []


async def test_claimable_orders_are_those_of_the_drivers_establishment_not_yet_taken(db_session, demo_tenant_slug):
    est, other = await _establishment(db_session), await _establishment(db_session)
    await _configure(db_session, est)
    me, rival = await _driver(db_session, est), await _driver(db_session, est)
    free = await _order(db_session, est, status="preparing")
    taken = await _order(db_session, est)
    await _order(db_session, other)  # autre etablissement
    await _order(db_session, est, order_type="pickup")
    await _order(db_session, est, status="pending", payment_status="pending")
    await _claim(db_session, rival, taken)

    result = await svc.claimable_orders(db_session, await _profile(db_session, me))
    assert result["enabled"] is True
    assert [o["order_id"] for o in result["orders"]] == [free.id]
    assert result["max_active_deliveries"] == 3 and result["active_deliveries"] == 0 and result["remaining_capacity"] == 3


async def test_claim_creates_a_self_assigned_delivery_with_its_event(db_session):
    est = await _establishment(db_session)
    await _configure(db_session, est)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est, status="preparing")
    order_id = order.id

    (delivery,) = await _claim(db_session, driver, order_id)
    assert delivery.status == "assigned" and delivery.driver_id == driver["id"]
    assert delivery.assigned_by_user_id == driver["user_id"]
    event = await db_session.scalar(select(DeliveryEvent).where(DeliveryEvent.order_id == order_id))
    assert event.event == "claimed" and event.actor_user_id == driver["user_id"]
    assert (await svc.driver_me(db_session, await _profile(db_session, driver)))["active_deliveries"] == 1


async def test_claim_refusals(db_session):
    est, other = await _establishment(db_session), await _establishment(db_session)
    driver = await _driver(db_session, est)
    order = await _order(db_session, est)

    # Dispatch coupe, puis mode comptoir.
    assert await _code(_claim(db_session, driver, order)) == "DISPATCH_DISABLED"
    await _settings(db_session, driver_dispatch_enabled=True)
    assert await _code(_claim(db_session, driver, order)) == "SELF_ASSIGN_DISABLED"

    await _configure(db_session, est, dispatch=False)
    absent = await _driver(db_session, est)
    absent_profile = await db_session.get(DriverProfile, absent["id"])
    clock = (await db_session.execute(select(TimeClockEntry).join(EmployeeProfile, EmployeeProfile.id == TimeClockEntry.employee_id).where(EmployeeProfile.user_id == absent["user_id"]))).scalar_one()
    clock.status, clock.clock_out_at = "closed", datetime.now(timezone.utc)
    await db_session.commit()
    assert await _code(_claim(db_session, absent, order)) == "DRIVER_NOT_CLOCKED_IN"
    assert absent_profile is not None

    assert await _code(_claim(db_session, driver, await _order(db_session, other))) == "DRIVER_WRONG_ESTABLISHMENT"
    assert await _code(_claim(db_session, driver, await _order(db_session, est, order_type="pickup"))) == "ORDER_NOT_DELIVERY"
    assert await _code(_claim(db_session, driver, await _order(db_session, est, status="pending", payment_status="pending"))) == "ORDER_NOT_ASSIGNABLE"
    assert await _code(_claim(db_session, driver, await _order(db_session, est, status="out_for_delivery"))) == "ORDER_NOT_ASSIGNABLE"
    assert await _code(_claim(db_session, driver, 987654)) == "ORDER_NOT_FOUND"
    assert await _code(svc.claim(db_session, await _profile(db_session, driver), [], user_id=1)) == "ORDER_IDS_INVALID"
    assert await _code(svc.claim(db_session, await _profile(db_session, driver), [order.id, order.id], user_id=1)) == "ORDER_IDS_INVALID"


async def test_a_taken_order_is_never_taken_over_by_a_claim(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _configure(db_session, est)
    owner, rival = await _driver(db_session, est), await _driver(db_session, est)
    order = await _order(db_session, est)
    order_id = order.id
    await svc.assign(db_session, order_ids=[order_id], driver_id=owner["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)

    assert await _code(_claim(db_session, rival, order_id)) == "ORDER_ALREADY_TAKEN"
    assert await _code(_claim(db_session, owner, order_id)) == "ORDER_ALREADY_TAKEN"  # meme pour son propre livreur
    delivery = await db_session.scalar(select(Delivery).where(Delivery.order_id == order_id))
    assert delivery.driver_id == owner["id"]  # inchangee : seul le comptoir reattribue


async def test_capacity_is_enforced_all_or_nothing_and_freed_by_delivery(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _configure(db_session, est, cap=2)
    driver = await _driver(db_session, est)
    orders = [await _order(db_session, est) for _ in range(4)]
    ids = [o.id for o in orders]

    await _claim(db_session, driver, ids[0])
    # Deux de plus feraient 3 : refus total, rien n'est cree.
    assert await _code(_claim(db_session, driver, ids[1], ids[2])) == "DRIVER_CAPACITY_REACHED"
    assert await db_session.scalar(select(func.count(Delivery.id)).where(Delivery.order_id.in_(ids[1:]))) == 0
    await _claim(db_session, driver, ids[1])  # la deuxieme passe : on est au plafond
    assert await _code(_claim(db_session, driver, ids[2])) == "DRIVER_CAPACITY_REACHED"

    # Partir puis livrer libere de la place ; une livraison en route compte tant qu'elle n'est pas livree.
    delivery_ids = [await db_session.scalar(select(Delivery.id).where(Delivery.order_id == i)) for i in ids[:2]]
    kwargs = {"user_id": driver["user_id"], "tenant_slug": demo_tenant_slug}
    await svc.driver_depart(db_session, await _profile(db_session, driver), delivery_ids, **kwargs)
    assert await _code(_claim(db_session, driver, ids[2])) == "DRIVER_CAPACITY_REACHED"
    await svc.driver_deliver(db_session, await _profile(db_session, driver), delivery_ids[0], cash_received=None, **kwargs)
    (third,) = await _claim(db_session, driver, ids[2])
    assert third.order_id == ids[2]


async def test_the_counter_can_still_assign_beyond_the_cap(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _configure(db_session, est, cap=1)
    driver = await _driver(db_session, est)
    first, second = await _order(db_session, est), await _order(db_session, est)
    await _claim(db_session, driver, first)
    assert await _code(_claim(db_session, driver, second)) == "DRIVER_CAPACITY_REACHED"

    result = await svc.assign(db_session, order_ids=[second.id], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)
    assert len(result) == 1  # le plafond encadre la prise libre, pas le jugement du comptoir


async def test_release_gives_the_order_back_only_before_departure_and_in_self_assign_mode(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    await _configure(db_session, est)
    me, other = await _driver(db_session, est), await _driver(db_session, est)
    held, started = await _order(db_session, est), await _order(db_session, est)
    await _claim(db_session, me, held)
    await _claim(db_session, me, started)
    ids = {o.id: await db_session.scalar(select(Delivery.id).where(Delivery.order_id == o.id)) for o in (held, started)}
    kwargs = {"user_id": me["user_id"], "tenant_slug": demo_tenant_slug}
    await svc.driver_depart(db_session, await _profile(db_session, me), [ids[started.id]], **kwargs)

    async def release(driver, delivery_id):
        return await svc.release(db_session, await _profile(db_session, driver), delivery_id, user_id=driver["user_id"])

    assert await _code(release(other, ids[held.id])) == "DELIVERY_NOT_FOUND"  # pas la sienne
    assert await _code(release(me, ids[started.id])) == "DELIVERY_ALREADY_STARTED"
    released = await release(me, ids[held.id])
    assert released.status == "cancelled"
    assert held.id in [o["order_id"] for o in (await svc.claimable_orders(db_session, await _profile(db_session, other)))["orders"]]
    events = [e.event for e in (await db_session.execute(select(DeliveryEvent).where(DeliveryEvent.order_id == held.id).order_by(DeliveryEvent.id))).scalars()]
    assert events == ["claimed", "released_by_driver"]

    # En mode comptoir, le livreur ne retire pas une attribution du comptoir.
    est2 = await _establishment(db_session)
    counter_driver = await _driver(db_session, est2)
    order = await _order(db_session, est2)
    await svc.assign(db_session, order_ids=[order.id], driver_id=counter_driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)
    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order.id))
    assert await _code(release(counter_driver, delivery_id)) == "SELF_ASSIGN_DISABLED"


# --------------------------------------------------------------------------- concurrence reelle


async def test_two_drivers_racing_for_the_same_order_get_exactly_one_winner(bootstrap_default_tenant):
    """Sessions reelles et distinctes (pas le savepoint du test) : le verrou et l'index unique tranchent."""
    slug = bootstrap_default_tenant["tenant_slug"]
    suffix = uuid.uuid4().hex[:8]
    created: dict = {"users": [], "orders": []}
    previous_flag = None
    try:
        async with get_tenant_session(slug) as session:
            est = Establishment(name=f"Race {suffix}", timezone="Europe/Paris", is_active=True)
            session.add(est)
            await session.commit()
            created["est"] = est.id
            row = await delivery_service.get_delivery_settings(session)
            previous_flag = row.driver_dispatch_enabled
            row.driver_dispatch_enabled = True
            await session.commit()
            await dispatch_settings.update(session, est.id, {"dispatch_mode": "self_assign", "max_active_deliveries": 1}, expected_version=0, user_id=1, user_email=None)
            drivers = []
            for i in range(2):
                out = await svc.create_driver(session, email=f"race{i}-{suffix}@test.fr", full_name=f"Racer {i}", phone=None, vehicle=None, establishment_id=est.id)
                emp = await session.scalar(select(EmployeeProfile).where(EmployeeProfile.user_id == out["user_id"]))
                session.add(TimeClockEntry(employee_id=emp.id, establishment_id=est.id, clock_in_at=datetime.now(timezone.utc), method="web", status="open"))
                await session.commit()
                drivers.append(out)
                created["users"].append(out["user_id"])
            orders = []
            for _ in range(3):
                order = Order(user_id=None, establishment_id=est.id, status="ready", payment_status="paid", order_type="delivery", total=20, subtotal=20, delivery_address="1 rue X")
                session.add(order)
                await session.commit()
                orders.append(order.id)
            created["orders"] = orders

        async def attempt(driver_out, order_ids):
            async with get_tenant_session(slug) as session:
                profile = await session.get(DriverProfile, driver_out["id"])
                try:
                    await svc.claim(session, profile, order_ids, user_id=driver_out["user_id"])
                    return "ok"
                except AppError as exc:
                    return exc.code

        # Meme commande, deux livreurs en meme temps.
        results = await asyncio.gather(attempt(drivers[0], [orders[0]]), attempt(drivers[1], [orders[0]]))
        assert sorted(results) == ["ORDER_ALREADY_TAKEN", "ok"], results

        # Un meme livreur (celui qui n'a rien) lance deux prises en parallele avec un plafond de 1 : une seule passe.
        loser = drivers[results.index("ok") ^ 1]
        results = await asyncio.gather(attempt(loser, [orders[1]]), attempt(loser, [orders[2]]))
        assert sorted(results) == ["DRIVER_CAPACITY_REACHED", "ok"], results
        async with get_tenant_session(slug) as session:
            per_order = (await session.execute(select(Delivery.order_id, func.count(Delivery.id)).where(Delivery.order_id.in_(orders)).group_by(Delivery.order_id))).all()
            assert all(count == 1 for _, count in per_order)  # jamais deux livraisons vivantes pour une commande
            assert await session.scalar(select(func.count(Delivery.id)).where(Delivery.driver_id == loser["id"])) == 1
    finally:
        async with get_tenant_session(slug) as session:
            order_ids = created["orders"]
            if order_ids:
                await session.execute(delete(DeliveryEvent).where(DeliveryEvent.order_id.in_(order_ids)))
                await session.execute(delete(Delivery).where(Delivery.order_id.in_(order_ids)))
                await session.execute(delete(Order).where(Order.id.in_(order_ids)))
            users = created["users"]
            if users:
                emp_ids = select(EmployeeProfile.id).where(EmployeeProfile.user_id.in_(users))
                await session.execute(delete(TimeClockEntry).where(TimeClockEntry.employee_id.in_(emp_ids)))
                await session.execute(delete(DriverProfile).where(DriverProfile.user_id.in_(users)))
                await session.execute(delete(EmployeeProfile).where(EmployeeProfile.user_id.in_(users)))
                await session.execute(delete(User).where(User.id.in_(users)))
            if "est" in created:
                await session.execute(delete(EstablishmentDispatchSettings).where(EstablishmentDispatchSettings.establishment_id == created["est"]))
                await session.execute(delete(Establishment).where(Establishment.id == created["est"]))
            if previous_flag is not None:
                await session.execute(update(RestaurantDeliverySettings).values(driver_dispatch_enabled=previous_flag))
            await session.commit()


# --------------------------------------------------------------------------- estimation dynamique


def test_departure_minutes_combines_travel_zone_and_stops():
    paris, near = (48.8566, 2.3522), (48.8666, 2.3522)  # ~1,1 km
    base = estimates.departure_minutes(origin=paris, destination=near, zone_minutes=30, other_stops=0)
    assert base == estimates.eta_minutes(*paris, *near) and base < 30  # le trajet reel prime sur la zone
    assert estimates.departure_minutes(origin=paris, destination=near, zone_minutes=30, other_stops=2) == base + 8
    assert estimates.departure_minutes(origin=(None, None), destination=near, zone_minutes=30, other_stops=0) == 30
    assert estimates.departure_minutes(origin=(None, None), destination=near, zone_minutes=30, other_stops=1) == 34
    assert estimates.departure_minutes(origin=(None, None), destination=(None, None), zone_minutes=None, other_stops=3) is None


async def _geo_order(session, est_id, *, status="ready", dest=(48.90, 2.35), estimate_minutes=45, **kwargs):
    order = await base._order(session, est_id, status=status, **kwargs)
    order.delivery_lat, order.delivery_lng = dest
    order.estimated_delivery_at = datetime.now(timezone.utc) + timedelta(minutes=estimate_minutes)
    await session.commit()
    return order


async def test_departure_recomputes_the_estimate_from_the_restaurant_position(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    record = await db_session.get(Establishment, est.id)
    record.latitude, record.longitude = 48.8566, 2.3522
    await db_session.commit()
    driver = await _driver(db_session, est)
    order = await _geo_order(db_session, est)
    order_id = order.id
    await svc.assign(db_session, order_ids=[order_id], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)

    before = datetime.now(timezone.utc)
    await orders_service.update_status(db_session, order_id, "out_for_delivery", tenant_slug=demo_tenant_slug)

    updated = await db_session.get(Order, order_id, populate_existing=True)
    expected = estimates.eta_minutes(48.8566, 2.3522, 48.90, 2.35)
    delta = (updated.estimated_delivery_at - before).total_seconds() / 60
    assert expected - 1 <= delta <= expected + 1  # ~ 4-5 km : bien moins que les 45 min d'origine
    assert expected < 45


async def test_a_run_adds_a_stop_overhead_for_the_other_orders(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    record = await db_session.get(Establishment, est.id)
    record.latitude, record.longitude = 48.8566, 2.3522
    await db_session.commit()
    driver = await _driver(db_session, est)
    first, second = await _geo_order(db_session, est), await _geo_order(db_session, est)
    ids = [first.id, second.id]
    await svc.assign(db_session, order_ids=ids, driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)

    await orders_service.update_status(db_session, ids[0], "out_for_delivery", tenant_slug=demo_tenant_slug)
    await orders_service.update_status(db_session, ids[1], "out_for_delivery", tenant_slug=demo_tenant_slug)

    one = await db_session.get(Order, ids[0], populate_existing=True)
    two = await db_session.get(Order, ids[1], populate_existing=True)
    # La 2e commande part avec la 1re encore en route : un arret de plus sur la tournee.
    assert (two.estimated_delivery_at - one.estimated_delivery_at).total_seconds() >= estimates.STOP_OVERHEAD_MINUTES * 60 - 5


async def test_without_position_or_zone_the_original_estimate_is_kept(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    order = await base._order(db_session, est)
    order.estimated_delivery_at = datetime.now(timezone.utc) + timedelta(minutes=33)
    original = order.estimated_delivery_at
    order_id = order.id
    await db_session.commit()

    await orders_service.update_status(db_session, order_id, "out_for_delivery", tenant_slug=demo_tenant_slug)  # dispatch coupe, aucun livreur

    assert (await db_session.get(Order, order_id, populate_existing=True)).estimated_delivery_at == original


async def test_legacy_departure_without_driver_also_refreshes_the_estimate(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    record = await db_session.get(Establishment, est.id)
    record.latitude, record.longitude = 48.8566, 2.3522
    await db_session.commit()
    order = await _geo_order(db_session, est)
    order_id = order.id

    await orders_service.update_status(db_session, order_id, "out_for_delivery", tenant_slug=demo_tenant_slug)

    updated = await db_session.get(Order, order_id, populate_existing=True)
    assert updated.estimated_delivery_at < datetime.now(timezone.utc) + timedelta(minutes=20)


# --------------------------------------------------------------------------- isolation HTTP


async def test_self_assign_routes_are_role_isolated(client, authed_client, bootstrap_default_tenant):
    from app.core.auth.security import create_access_token

    slug = bootstrap_default_tenant["tenant_slug"]
    suffix = uuid.uuid4().hex[:8]
    est_id = user_id = None
    base_claims = {"tenant_id": bootstrap_default_tenant["tenant_id"], "tenant_slug": slug, "must_change_password": False}
    try:
        async with get_tenant_session(slug) as session:
            est = Establishment(name=f"HTTP6 {suffix}", timezone="Europe/Paris", is_active=True)
            session.add(est)
            await session.commit()
            est_id = est.id
            out = await svc.create_driver(session, email=f"h6-{suffix}@test.fr", full_name="H6", phone=None, vehicle=None, establishment_id=est_id)
            user_id = out["user_id"]
        driver = {"Authorization": "Bearer " + create_access_token({**base_claims, "sub": str(user_id), "email": out["email"], "role": "driver", "permissions": []})}
        staff = {"Authorization": "Bearer " + create_access_token({**base_claims, "sub": str(bootstrap_default_tenant["staff_user_id"]), "email": "staff@test.com", "role": "staff", "permissions": ["*"]})}

        # Le livreur voit sa liste (vide en mode comptoir) et son plafond ; il ne touche jamais aux reglages.
        available = await client.get("/api/v1/delivery/driver/available", headers=driver)
        assert available.status_code == 200 and available.json()["orders"] == []
        me = (await client.get("/api/v1/delivery/driver/me", headers=driver)).json()
        assert me["dispatch_mode"] == "counter" and me["max_active_deliveries"] == 3 and me["remaining_capacity"] == 3
        url = f"/api/v1/delivery/establishments/{est_id}/dispatch-settings"
        assert (await client.get(url, headers=driver)).status_code in (403, 404)
        assert (await client.put(url, headers=driver, json={"expected_version": 0, "dispatch_mode": "self_assign"})).status_code in (403, 404)
        refused = await client.post("/api/v1/delivery/driver/claim", headers=driver, json={"order_ids": [1]})
        assert refused.status_code == 409  # dispatch coupe ou mode comptoir : jamais de prise

        # Le personnel ne se fait pas passer pour un livreur.
        for method, path, body in [
            ("GET", "/api/v1/delivery/driver/available", None),
            ("POST", "/api/v1/delivery/driver/claim", {"order_ids": [1]}),
            ("POST", "/api/v1/delivery/driver/deliveries/1/release", None),
        ]:
            assert (await client.request(method, path, headers=staff, json=body)).status_code == 403, path

        # Sans permission en base, le personnel ne lit ni ne modifie ; l'admin le peut, avec controle de version.
        assert (await client.get(url, headers=staff)).status_code == 403
        assert (await authed_client.get(url)).json()["version"] == 0
        assert (await client.put(url, headers=staff, json={"expected_version": 0, "dispatch_mode": "self_assign"})).status_code == 403
        ok = await authed_client.put(url, json={"expected_version": 0, "dispatch_mode": "self_assign", "max_active_deliveries": 2})
        assert ok.status_code == 200 and ok.json()["dispatch_mode"] == "self_assign" and ok.json()["version"] == 1
        stale = await authed_client.put(url, json={"expected_version": 0, "max_active_deliveries": 4})
        assert stale.status_code == 409
        bad = await authed_client.put(url, json={"expected_version": 1, "max_active_deliveries": 99})
        assert bad.status_code == 422
        me = (await client.get("/api/v1/delivery/driver/me", headers=driver)).json()
        assert me["dispatch_mode"] == "self_assign" and me["max_active_deliveries"] == 2
    finally:
        async with get_tenant_session(slug) as session:
            if user_id is not None:
                await session.execute(delete(TimeClockEntry).where(TimeClockEntry.establishment_id == est_id))
                await session.execute(delete(DriverProfile).where(DriverProfile.user_id == user_id))
                await session.execute(delete(EmployeeProfile).where(EmployeeProfile.user_id == user_id))
                await session.execute(delete(User).where(User.id == user_id))
            if est_id is not None:
                await session.execute(delete(RestaurantDeliverySettingsAudit).where(RestaurantDeliverySettingsAudit.field_name.like(f"establishment_{est_id}.%")))
                await session.execute(delete(EstablishmentDispatchSettings).where(EstablishmentDispatchSettings.establishment_id == est_id))
                await session.execute(delete(Establishment).where(Establishment.id == est_id))
            await session.commit()
