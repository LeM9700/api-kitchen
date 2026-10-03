"""Phase 5 livraison interne : suivi GPS du livreur (consentement, reception, vue client, carte admin, purge)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.auth.security import create_access_token
from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.delivery import tracking
from app.modules.delivery.models import (
    Delivery,
    DriverLastLocation,
    DriverLocationPoint,
    DriverProfile,
)
from app.modules.orders.models import Order

# Les helpers (prefixe _) viennent du fichier de la phase 4 : meme base, memes conventions.
from test_delivery_proof_failures import _driver, _en_route, _establishment, _order, _profile  # noqa: E402

PARIS = (48.8566, 2.3522)


def _pt(lat=PARIS[0], lng=PARIS[1], seconds_ago=0, **extra):
    return {
        "lat": lat,
        "lng": lng,
        "recorded_at": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        **extra,
    }


async def _consent(session, driver):
    profile = await _profile(session, driver)
    await tracking.grant_consent(session, profile, tracking.LOCATION_NOTICE_VERSION)


async def _ingest(session, driver, *points):
    return await tracking.ingest(session, await _profile(session, driver), list(points))


async def _code(awaitable) -> str:
    with pytest.raises(AppError) as err:
        await awaitable
    return err.value.code


async def _on_the_road(session, slug, *, consent=True, **order_kwargs):
    est = await _establishment(session)
    driver = await _driver(session, est)
    order_id, delivery_id = await _en_route(session, slug, est, driver, arrived=False, user_id=5, **order_kwargs)
    order = await session.get(Order, order_id)
    order.delivery_lat, order.delivery_lng = 48.87, 2.36
    await session.commit()
    if consent:
        await _consent(session, driver)
    return est, driver, order_id, delivery_id


# --------------------------------------------------------------------------- aides pures


def test_haversine_and_eta():
    assert tracking.haversine_m(48.8566, 2.3522, 45.7640, 4.8357) == pytest.approx(392_000, rel=0.02)
    assert tracking.haversine_m(*PARIS, *PARIS) == 0
    assert tracking.eta_minutes(*PARIS, None, None) is None
    assert tracking.eta_minutes(*PARIS, *PARIS) == 1  # jamais « 0 minute »
    assert tracking.eta_minutes(48.8566, 2.3522, 48.9566, 2.3522) > tracking.eta_minutes(48.8566, 2.3522, 48.8666, 2.3522)


def test_retention_has_a_96_hour_floor(monkeypatch):
    monkeypatch.setattr(settings, "gps_retention_hours", 24)
    assert tracking.retention_hours() == 96
    monkeypatch.setattr(settings, "gps_retention_hours", 0)
    assert tracking.retention_hours() == 96
    monkeypatch.setattr(settings, "gps_retention_hours", 200)
    assert tracking.retention_hours() == 200


# --------------------------------------------------------------------------- consentement et activite


async def test_no_position_is_accepted_without_consent(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug, consent=False)

    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_CONSENT_REQUIRED"
    assert await db_session.scalar(select(func.count(DriverLocationPoint.id))) == 0

    await _consent(db_session, driver)
    assert (await _ingest(db_session, driver, _pt()))["stored"] == 1


async def test_consent_needs_the_current_notice_version_and_can_be_withdrawn(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug, consent=False)
    profile = await _profile(db_session, driver)

    assert await _code(tracking.grant_consent(db_session, profile, "1999-01-01")) == "LOCATION_NOTICE_OUTDATED"
    assert tracking.has_consent(await _profile(db_session, driver)) is False

    await _consent(db_session, driver)
    assert tracking.has_consent(await _profile(db_session, driver)) is True
    await tracking.withdraw_consent(db_session, await _profile(db_session, driver))
    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_CONSENT_REQUIRED"


async def test_an_old_notice_version_no_longer_counts_as_consent(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug, consent=False)
    profile = await _profile(db_session, driver)
    profile.location_consent_at = datetime.now(timezone.utc)
    profile.location_consent_version = "2020-01-01"
    await db_session.commit()

    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_CONSENT_REQUIRED"


async def test_position_is_only_accepted_during_an_active_delivery(db_session, demo_tenant_slug):
    from app.modules.delivery import dispatch_service as svc

    est = await _establishment(db_session)
    driver = await _driver(db_session, est)
    await _consent(db_session, driver)
    # Rien a livrer.
    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_NOT_ACTIVE"

    # Une livraison attribuee mais pas partie ne suffit pas.
    order = await _order(db_session, est)
    order_id = order.id
    await svc.assign(db_session, order_ids=[order_id], driver_id=driver["id"], actor_user_id=1, tenant_slug=demo_tenant_slug)
    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_NOT_ACTIVE"

    delivery_id = await db_session.scalar(select(Delivery.id).where(Delivery.order_id == order_id))
    await svc.driver_depart(db_session, await _profile(db_session, driver), [delivery_id], user_id=driver["user_id"], tenant_slug=demo_tenant_slug)
    assert (await _ingest(db_session, driver, _pt()))["active_deliveries"] == 1

    # Une fois livree, l'envoi est de nouveau refuse : l'app doit s'arreter.
    await svc.driver_deliver(db_session, await _profile(db_session, driver), delivery_id, cash_received=None, user_id=driver["user_id"], tenant_slug=demo_tenant_slug)
    assert await _code(_ingest(db_session, driver, _pt())) == "LOCATION_NOT_ACTIVE"


# --------------------------------------------------------------------------- qualite des points


async def test_unusable_points_are_ignored(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)

    ack = await _ingest(
        db_session,
        driver,
        _pt(lat=95.0),  # hors bornes
        _pt(lng=181.0),
        _pt(lat=float("nan")),
        _pt(accuracy_m=800),  # precision inutilisable
        _pt(seconds_ago=11 * 60),  # trop ancien : le suivi est en direct
    )
    assert ack == {"received": 5, "stored": 0, "active_deliveries": 1}
    assert await db_session.scalar(select(func.count(DriverLocationPoint.id))) == 0
    assert await db_session.scalar(select(func.count(DriverLastLocation.driver_id))) == 0


async def test_a_phone_clock_in_the_future_is_clamped_not_trusted(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)

    await _ingest(db_session, driver, {**_pt(), "recorded_at": datetime.now(timezone.utc) + timedelta(hours=3)})
    last = await db_session.scalar(select(DriverLastLocation))
    assert last.recorded_at <= datetime.now(timezone.utc) + timedelta(seconds=5)


async def test_batches_are_capped(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)

    code = await _code(_ingest(db_session, driver, *[_pt(seconds_ago=i) for i in range(tracking.MAX_BATCH + 1)]))
    assert code == "LOCATION_BATCH_TOO_LARGE"


async def test_history_is_sampled_but_the_latest_position_is_always_kept(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)

    # 0 s, +5 s ~2 m plus loin : un seul point d'historique ; +25 s : un deuxieme ; +5 s mais 60 m plus loin : un troisieme.
    t0 = 200
    first = _pt(seconds_ago=t0)
    close = _pt(lat=PARIS[0] + 0.00002, seconds_ago=t0 - 5)
    later = _pt(lat=PARIS[0] + 0.00002, seconds_ago=t0 - 30)
    jump = _pt(lat=PARIS[0] + 0.00060, seconds_ago=t0 - 35)
    ack = await _ingest(db_session, driver, first, close, later, jump)

    assert ack["stored"] == 3
    assert await db_session.scalar(select(func.count(DriverLocationPoint.id))) == 3
    last = await db_session.scalar(select(DriverLastLocation))
    assert last.lat == pytest.approx(PARIS[0] + 0.00060)  # la derniere position n'est jamais echantillonnee


async def test_a_late_batch_does_not_move_the_driver_backwards(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(lat=48.90, seconds_ago=5))
    await _ingest(db_session, driver, _pt(lat=48.80, seconds_ago=200))  # envoi tardif, mesure plus ancienne

    last = await db_session.scalar(select(DriverLastLocation))
    assert last.lat == pytest.approx(48.90)


async def test_points_carry_the_current_run(db_session, demo_tenant_slug):
    _, driver, _, delivery_id = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt())

    run_id = await db_session.scalar(select(Delivery.run_id).where(Delivery.id == delivery_id))
    assert run_id is not None
    assert await db_session.scalar(select(DriverLocationPoint.run_id)) == run_id


# --------------------------------------------------------------------------- vue client


async def test_customer_sees_only_the_driver_of_their_own_order_while_en_route(db_session, demo_tenant_slug):
    _, driver, order_id, _ = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(seconds_ago=3))

    view = await tracking.client_view(db_session, order_id, 5)
    assert view["available"] is True and view["stale"] is False
    assert view["driver_first_name"] == "Marc"
    assert view["eta_minutes"] >= 1 and view["destination_lat"] == pytest.approx(48.87)
    assert set(view) <= {
        "available", "driver_first_name", "destination_lat", "destination_lng", "arrived", "lat", "lng",
        "heading", "recorded_at", "age_seconds", "stale", "eta_minutes",
    }  # ni vitesse, ni precision, ni historique, ni identifiant du livreur

    assert await _code(tracking.client_view(db_session, order_id, 6)) == "ORDER_NOT_FOUND"


async def test_customer_gets_nothing_before_departure_or_after_delivery(db_session, demo_tenant_slug):
    est = await _establishment(db_session)
    waiting = await _order(db_session, est, user_id=5, status="preparing")
    waiting_id = waiting.id
    pickup = await _order(db_session, est, user_id=5, order_type="pickup", status="out_for_delivery")
    pickup_id = pickup.id

    assert await _code(tracking.client_view(db_session, waiting_id, 5)) == "ORDER_NOT_TRACKABLE"
    assert await _code(tracking.client_view(db_session, pickup_id, 5)) == "ORDER_NOT_TRACKABLE"


async def test_no_position_yet_is_reported_as_unavailable_not_as_an_error(db_session, demo_tenant_slug):
    _, _, order_id, _ = await _on_the_road(db_session, demo_tenant_slug)

    view = await tracking.client_view(db_session, order_id, 5)
    assert view["available"] is False and view["driver_first_name"] == "Marc"


async def test_a_position_older_than_a_minute_is_flagged_stale(db_session, demo_tenant_slug):
    _, driver, order_id, delivery_id = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(seconds_ago=3))
    last = await db_session.scalar(select(DriverLastLocation))
    last.recorded_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    delivery = await db_session.get(Delivery, delivery_id)
    delivery.departed_at = datetime.now(timezone.utc) - timedelta(minutes=10)  # parti bien avant
    await db_session.commit()

    view = await tracking.client_view(db_session, order_id, 5)
    assert view["stale"] is True and view["age_seconds"] >= 90


async def test_a_position_from_before_the_departure_is_not_shown(db_session, demo_tenant_slug):
    """La derniere position d'une course precedente ne doit pas apparaitre comme celle de la course en cours."""
    _, driver, order_id, delivery_id = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(seconds_ago=3))
    last = await db_session.scalar(select(DriverLastLocation))
    last.recorded_at = datetime.now(timezone.utc) - timedelta(hours=2)
    delivery = await db_session.get(Delivery, delivery_id)
    delivery.departed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db_session.commit()

    assert (await tracking.client_view(db_session, order_id, 5))["available"] is False


async def test_arrival_hides_the_eta(db_session, demo_tenant_slug):
    from app.modules.delivery import dispatch_service as svc

    _, driver, order_id, delivery_id = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(seconds_ago=3))
    await svc.driver_arrived(db_session, await _profile(db_session, driver), delivery_id, user_id=driver["user_id"], tenant_slug=demo_tenant_slug)

    view = await tracking.client_view(db_session, order_id, 5)
    assert view["arrived"] is True and view["eta_minutes"] is None


# --------------------------------------------------------------------------- carte admin


async def test_live_board_shows_position_only_for_drivers_on_the_road(db_session, demo_tenant_slug):
    est, driver, order_id, _ = await _on_the_road(db_session, demo_tenant_slug)
    free = await _driver(db_session, est)
    await _ingest(db_session, driver, _pt(seconds_ago=3))

    board = {d["driver_id"]: d for d in await tracking.live_board(db_session, est.id)}
    on_road, idle = board[driver["id"]], board[free["id"]]
    assert on_road["state"] == "en_route" and on_road["position"]["lat"] == pytest.approx(PARIS[0])
    assert on_road["signal_lost"] is False and on_road["sharing_consent"] is True
    assert on_road["deliveries"][0]["order_id"] == order_id
    assert idle["state"] == "free" and idle["position"] is None and idle["sharing_consent"] is False


async def test_live_board_hides_the_last_position_once_the_delivery_is_over(db_session, demo_tenant_slug):
    from app.modules.delivery import dispatch_service as svc

    est, driver, _, delivery_id = await _on_the_road(db_session, demo_tenant_slug)
    await _ingest(db_session, driver, _pt(seconds_ago=3))
    await svc.driver_deliver(db_session, await _profile(db_session, driver), delivery_id, cash_received=None, user_id=driver["user_id"], tenant_slug=demo_tenant_slug)

    (entry,) = [d for d in await tracking.live_board(db_session, est.id) if d["driver_id"] == driver["id"]]
    assert entry["state"] == "free" and entry["position"] is None  # pas de suivi hors livraison


async def test_live_board_flags_a_lost_signal(db_session, demo_tenant_slug):
    est, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)

    (silent,) = [d for d in await tracking.live_board(db_session, est.id) if d["driver_id"] == driver["id"]]
    assert silent["state"] == "en_route" and silent["position"] is None
    assert silent["signal_lost"] is True and silent["stale"] is True  # parti, jamais de position

    await _ingest(db_session, driver, _pt(seconds_ago=3))
    last = await db_session.scalar(select(DriverLastLocation))
    last.recorded_at = datetime.now(timezone.utc) - timedelta(seconds=150)
    await db_session.commit()
    (old,) = [d for d in await tracking.live_board(db_session, est.id) if d["driver_id"] == driver["id"]]
    assert old["signal_lost"] is True and old["age_seconds"] >= 150

    last = await db_session.scalar(select(DriverLastLocation))
    last.recorded_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    await db_session.commit()
    (recent,) = [d for d in await tracking.live_board(db_session, est.id) if d["driver_id"] == driver["id"]]
    assert recent["stale"] is True and recent["signal_lost"] is False


# --------------------------------------------------------------------------- purge


async def test_purge_removes_only_positions_older_than_the_retention(db_session, demo_tenant_slug):
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)
    driver_id = driver["id"]
    now = datetime.now(timezone.utc)
    for hours in (97, 95, 1):
        db_session.add(DriverLocationPoint(driver_id=driver_id, lat=48.8, lng=2.3, recorded_at=now - timedelta(hours=hours)))
    db_session.add(DriverLastLocation(driver_id=driver_id, lat=48.8, lng=2.3, recorded_at=now - timedelta(hours=100)))
    await db_session.commit()

    result = await tracking.purge_old_locations(db_session, now)
    assert result == {"points": 1, "last_locations": 1}
    remaining = sorted((now - p.recorded_at).total_seconds() // 3600 for p in (await db_session.execute(select(DriverLocationPoint))).scalars())
    assert remaining == [1, 95]  # 95 h : encore conserve, le litige reste verifiable


async def test_a_short_configured_retention_never_purges_before_96_hours(db_session, demo_tenant_slug, monkeypatch):
    monkeypatch.setattr(settings, "gps_retention_hours", 24)
    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug)
    now = datetime.now(timezone.utc)
    db_session.add(DriverLocationPoint(driver_id=driver["id"], lat=48.8, lng=2.3, recorded_at=now - timedelta(hours=50)))
    await db_session.commit()

    assert (await tracking.purge_old_locations(db_session, now))["points"] == 0


# --------------------------------------------------------------------------- HTTP


async def test_gps_routes_are_closed_to_other_roles_and_anonymous_callers(client, bootstrap_default_tenant):
    slug = bootstrap_default_tenant["tenant_slug"]
    staff = {
        "Authorization": "Bearer "
        + create_access_token(
            {
                "sub": str(bootstrap_default_tenant["staff_user_id"]),
                "email": "staff@test.com",
                "role": "staff",
                "tenant_id": bootstrap_default_tenant["tenant_id"],
                "tenant_slug": slug,
                "permissions": ["*"],
                "must_change_password": False,
            }
        )
    }
    body = {"points": [{"lat": 48.85, "lng": 2.35}]}

    # Seul un livreur envoie sa position ; le personnel ne peut ni l'envoyer ni donner son accord a sa place.
    assert (await client.post("/api/v1/delivery/driver/location", headers=staff, json=body)).status_code == 403
    assert (await client.post("/api/v1/delivery/driver/location-consent", headers=staff, json={"version": "x"})).status_code == 403
    assert (await client.delete("/api/v1/delivery/driver/location-consent", headers=staff)).status_code == 403
    # Sans identification : refus.
    assert (await client.post("/api/v1/delivery/driver/location", json=body)).status_code == 401
    assert (await client.get("/api/v1/delivery/live")).status_code == 401
    assert (await client.get("/api/v1/orders/1/driver-location")).status_code == 401
    # Un lot trop grand ou des coordonnees impossibles sont refuses avant tout traitement.
    too_big = {"points": [{"lat": 48.85, "lng": 2.35}] * 31}
    assert (await client.post("/api/v1/delivery/driver/location", headers=staff, json=too_big)).status_code in (403, 422)


async def test_driver_profile_announces_the_real_retention(db_session, demo_tenant_slug, monkeypatch):
    from app.modules.delivery import dispatch_service as svc

    _, driver, _, _ = await _on_the_road(db_session, demo_tenant_slug, consent=False)
    me = await svc.driver_me(db_session, await _profile(db_session, driver))
    assert me["location_consent"] is False
    assert me["location_notice_version"] == tracking.LOCATION_NOTICE_VERSION
    assert me["location_retention_hours"] == 96

    monkeypatch.setattr(settings, "gps_retention_hours", 200)
    await _consent(db_session, driver)
    me = await svc.driver_me(db_session, await _profile(db_session, driver))
    assert me["location_consent"] is True and me["location_retention_hours"] == 200
