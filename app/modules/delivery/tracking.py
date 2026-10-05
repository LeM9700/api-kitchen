"""Suivi GPS des livreurs pendant une livraison.

Garde-fous (voir ``PRIVACY.md`` et le plan du sprint) :
- **consentement** du livreur, versionne, retirable ; sans lui aucune position n'est acceptee ;
- **uniquement pendant une livraison active** (``out_for_delivery`` ou ``arrived``) : en dehors, l'envoi est refuse
  (l'app s'arrete) et la derniere position n'est jamais montree ;
- le **client** ne voit que la position du livreur de **sa** commande, tant qu'elle est ``out_for_delivery`` ;
- historique **echantillonne** (au plus un point toutes les 20 s, ou tous les 30 m) et **purge** apres
  ``gps_retention_hours`` (plancher 96 h) ;
- les positions de plus de 10 minutes ou impossibles (hors bornes, precision inutilisable) sont ignorees.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.auth.models import User
from app.modules.delivery import lifecycle
from app.modules.delivery.estimates import AVERAGE_SPEED_MPS, ROAD_FACTOR, eta_minutes, haversine_m  # noqa: F401
from app.modules.delivery.models import (
    Delivery,
    DriverLastLocation,
    DriverLocationPoint,
    DriverProfile,
)
from app.modules.hr.models import EmployeeProfile, TimeClockEntry
from app.modules.orders.models import Order

# Version du texte d'information montre au livreur ; la changer redemande son accord.
# 2026-10-03-bg : le texte annonce desormais le partage en arriere-plan / ecran verrouille.
LOCATION_NOTICE_VERSION = "2026-10-03-bg"

MAX_BATCH = 30
MAX_POINT_AGE = timedelta(minutes=10)
MAX_FUTURE_SKEW = timedelta(seconds=60)
MAX_USABLE_ACCURACY_M = 500.0
MIN_SAMPLE_SECONDS = 20
MIN_SAMPLE_DISTANCE_M = 30.0
STALE_AFTER_SECONDS = 60
SIGNAL_LOST_SECONDS = 120
MIN_RETENTION_HOURS = 96
EN_ROUTE_STATUSES = ("out_for_delivery", "arrived")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def retention_hours() -> int:
    return max(MIN_RETENTION_HOURS, int(settings.gps_retention_hours))


# --------------------------------------------------------------------------- consentement


async def grant_consent(session: AsyncSession, driver: DriverProfile, version: str) -> None:
    if version != LOCATION_NOTICE_VERSION:
        raise AppError(
            "LOCATION_NOTICE_OUTDATED",
            "Le texte d'information a change : mettez l'application a jour puis reessayez.",
            409,
            "version",
        )
    driver.location_consent_at = _now()
    driver.location_consent_version = version
    await session.commit()


async def withdraw_consent(session: AsyncSession, driver: DriverProfile) -> None:
    """Le livreur retire son accord : plus aucune position n'est acceptee a partir de maintenant."""
    driver.location_consent_at = None
    driver.location_consent_version = None
    await session.commit()


def has_consent(driver: DriverProfile) -> bool:
    return driver.location_consent_at is not None and driver.location_consent_version == LOCATION_NOTICE_VERSION


# --------------------------------------------------------------------------- reception des positions


def _clean_points(raw_points: list[dict], now: datetime) -> list[dict]:
    clean: list[dict] = []
    for point in raw_points:
        lat, lng = point.get("lat"), point.get("lng")
        if lat is None or lng is None:
            continue
        if not (math.isfinite(lat) and math.isfinite(lng)) or abs(lat) > 90 or abs(lng) > 180:
            continue
        accuracy = point.get("accuracy_m")
        if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0 or accuracy > MAX_USABLE_ACCURACY_M):
            continue  # une position a plus de 500 m pres ne sert a rien
        recorded = point.get("recorded_at")
        recorded = _aware(recorded) if recorded is not None else now
        if recorded > now + MAX_FUTURE_SKEW:
            recorded = now  # horloge du telephone en avance
        if recorded < now - MAX_POINT_AGE:
            continue  # trop ancienne : le suivi est en direct, pas un journal rejoue
        clean.append({**point, "recorded_at": recorded})
    clean.sort(key=lambda p: p["recorded_at"])
    return clean


async def ingest(session: AsyncSession, driver: DriverProfile, raw_points: list[dict]) -> dict:
    """Enregistre un ou plusieurs points (le telephone peut en accumuler hors reseau et les renvoyer)."""
    if len(raw_points) > MAX_BATCH:
        raise AppError("LOCATION_BATCH_TOO_LARGE", f"{MAX_BATCH} positions au maximum par envoi.", 422)
    if not has_consent(driver):
        raise AppError(
            "LOCATION_CONSENT_REQUIRED",
            "Acceptez d'abord le partage de votre position pendant les livraisons.",
            403,
        )
    driver_id = driver.id
    active = list(
        (
            await session.execute(
                select(Delivery).where(Delivery.driver_id == driver_id, Delivery.status.in_(EN_ROUTE_STATUSES))
            )
        ).scalars()
    )
    if not active:
        raise AppError(
            "LOCATION_NOT_ACTIVE",
            "Aucune livraison en cours : la position n'est pas partagee.",
            409,
        )
    run_id = next((d.run_id for d in active if d.run_id is not None), None)

    now = _now()
    points = _clean_points(raw_points, now)
    if not points:
        return {"received": len(raw_points), "stored": 0, "active_deliveries": len(active)}

    previous = await session.scalar(
        select(DriverLocationPoint)
        .where(DriverLocationPoint.driver_id == driver_id)
        .order_by(DriverLocationPoint.recorded_at.desc())
        .limit(1)
    )
    prev = (
        (previous.lat, previous.lng, _aware(previous.recorded_at)) if previous is not None else None
    )
    stored = 0
    for point in points:
        keep = prev is None
        if prev is not None:
            seconds = (point["recorded_at"] - prev[2]).total_seconds()
            keep = seconds >= MIN_SAMPLE_SECONDS or haversine_m(prev[0], prev[1], point["lat"], point["lng"]) >= MIN_SAMPLE_DISTANCE_M
            keep = keep and seconds >= 0
        if keep:
            session.add(
                DriverLocationPoint(
                    driver_id=driver_id,
                    run_id=run_id,
                    lat=point["lat"],
                    lng=point["lng"],
                    accuracy_m=point.get("accuracy_m"),
                    speed_mps=point.get("speed_mps"),
                    heading=point.get("heading"),
                    recorded_at=point["recorded_at"],
                )
            )
            prev = (point["lat"], point["lng"], point["recorded_at"])
            stored += 1

    newest = points[-1]
    last = await session.get(DriverLastLocation, driver_id, with_for_update=True)
    if last is None:
        session.add(
            DriverLastLocation(
                driver_id=driver_id,
                run_id=run_id,
                lat=newest["lat"],
                lng=newest["lng"],
                accuracy_m=newest.get("accuracy_m"),
                speed_mps=newest.get("speed_mps"),
                heading=newest.get("heading"),
                recorded_at=newest["recorded_at"],
                received_at=now,
            )
        )
    elif _aware(last.recorded_at) <= newest["recorded_at"]:  # un lot en retard ne fait pas reculer la position
        last.run_id = run_id
        last.lat, last.lng = newest["lat"], newest["lng"]
        last.accuracy_m, last.speed_mps, last.heading = (
            newest.get("accuracy_m"),
            newest.get("speed_mps"),
            newest.get("heading"),
        )
        last.recorded_at, last.received_at = newest["recorded_at"], now
    await session.commit()
    return {"received": len(raw_points), "stored": stored, "active_deliveries": len(active)}


# --------------------------------------------------------------------------- vue client


async def client_view(session: AsyncSession, order_id: int, user_id: int) -> dict:
    """Position du livreur de **cette** commande, pour son client, tant qu'elle est en route."""
    order = await session.get(Order, order_id)
    # 404 pour la commande d'un autre : on ne revele pas qu'elle existe.
    if order is None or order.user_id is None or int(order.user_id) != int(user_id):
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.order_type != "delivery" or order.status != "out_for_delivery":
        raise AppError("ORDER_NOT_TRACKABLE", "Le suivi du livreur n'est disponible que pendant la livraison.", 409)
    delivery = await lifecycle.active_delivery(session, order_id)
    if delivery is None or delivery.status not in EN_ROUTE_STATUSES:
        raise AppError("ORDER_NOT_TRACKABLE", "Le suivi du livreur n'est disponible que pendant la livraison.", 409)

    driver = await session.get(DriverProfile, delivery.driver_id)
    user = await session.get(User, driver.user_id) if driver is not None else None
    first_name = (user.full_name or "").strip().split(" ")[0] if user and user.full_name else None
    base = {
        "driver_first_name": first_name or None,
        "destination_lat": order.delivery_lat,
        "destination_lng": order.delivery_lng,
        "arrived": delivery.status == "arrived",
    }
    last = await session.get(DriverLastLocation, delivery.driver_id)
    departed = _aware(delivery.departed_at) if delivery.departed_at else None
    # Une position anterieure au depart date d'une autre course : on ne la montre pas.
    if last is None or (departed is not None and _aware(last.recorded_at) < departed - timedelta(seconds=60)):
        return {**base, "available": False}
    age = max(0, int((_now() - _aware(last.recorded_at)).total_seconds()))
    return {
        **base,
        "available": True,
        "lat": last.lat,
        "lng": last.lng,
        "heading": last.heading,
        "recorded_at": last.recorded_at,
        "age_seconds": age,
        "stale": age > STALE_AFTER_SECONDS,
        "eta_minutes": None if delivery.status == "arrived" else eta_minutes(last.lat, last.lng, order.delivery_lat, order.delivery_lng),
    }


# --------------------------------------------------------------------------- carte admin


async def live_board(session: AsyncSession, establishment_id: int | None = None) -> list[dict]:
    """Tous les livreurs actifs : position (seulement pendant une livraison en route), signal, charge."""
    stmt = select(DriverProfile).where(DriverProfile.is_active.is_(True)).order_by(DriverProfile.id)
    if establishment_id is not None:
        stmt = stmt.where(DriverProfile.establishment_id == establishment_id)
    drivers = list((await session.execute(stmt)).scalars())
    if not drivers:
        return []
    ids = [d.id for d in drivers]
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_([d.user_id for d in drivers])))).scalars()}
    present = {
        user_id
        for (user_id,) in (
            await session.execute(
                select(EmployeeProfile.user_id)
                .join(TimeClockEntry, TimeClockEntry.employee_id == EmployeeProfile.id)
                .where(
                    EmployeeProfile.user_id.in_([d.user_id for d in drivers]),
                    TimeClockEntry.status.in_(("open", "break")),
                )
            )
        ).all()
    }
    lasts = {l.driver_id: l for l in (await session.execute(select(DriverLastLocation).where(DriverLastLocation.driver_id.in_(ids)))).scalars()}
    rows = (
        await session.execute(
            select(Delivery, Order)
            .join(Order, Order.id == Delivery.order_id)
            .where(Delivery.driver_id.in_(ids), Delivery.status.in_(("assigned",) + EN_ROUTE_STATUSES))
        )
    ).all()
    by_driver: dict[int, list[tuple[Delivery, Order]]] = {}
    for delivery, order in rows:
        by_driver.setdefault(delivery.driver_id, []).append((delivery, order))

    now = _now()
    out: list[dict] = []
    for driver in drivers:
        items = by_driver.get(driver.id, [])
        en_route = [pair for pair in items if pair[0].status in EN_ROUTE_STATUSES]
        last = lasts.get(driver.id)
        position = None
        age = None
        if en_route and last is not None:
            age = max(0, int((now - _aware(last.recorded_at)).total_seconds()))
            position = {"lat": last.lat, "lng": last.lng, "heading": last.heading, "recorded_at": last.recorded_at}
        stale = bool(en_route) and (age is None or age > STALE_AFTER_SECONDS)
        user = users.get(driver.user_id)
        out.append(
            {
                "driver_id": driver.id,
                "driver_name": user.full_name if user else None,
                "clocked_in": driver.user_id in present,
                "state": "en_route" if en_route else ("assigned" if items else "free"),
                "position": position,
                "age_seconds": age,
                "stale": stale,
                "signal_lost": bool(en_route) and (age is None or age > SIGNAL_LOST_SECONDS),
                "sharing_consent": has_consent(driver),
                "deliveries": [
                    {
                        "delivery_id": d.id,
                        "order_id": o.id,
                        "status": d.status,
                        "address": o.delivery_address,
                        "lat": o.delivery_lat,
                        "lng": o.delivery_lng,
                    }
                    for d, o in items
                ],
            }
        )
    return out


# --------------------------------------------------------------------------- purge


async def purge_old_locations(session: AsyncSession, now: datetime | None = None) -> dict:
    """Supprime l'historique et les dernieres positions plus vieux que la duree de conservation."""
    cutoff = (now or _now()) - timedelta(hours=retention_hours())
    points = await session.execute(delete(DriverLocationPoint).where(DriverLocationPoint.recorded_at < cutoff))
    lasts = await session.execute(delete(DriverLastLocation).where(DriverLastLocation.recorded_at < cutoff))
    await session.commit()
    return {"points": points.rowcount or 0, "last_locations": lasts.rowcount or 0}
