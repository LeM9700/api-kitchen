"""Synchronisation livraison <-> commande.

``orders.update_status`` reste l'unique chemin qui change le statut d'une commande ; ces fonctions
sont appelees depuis lui, dans sa transaction (avant le commit), pour que la table ``deliveries``
ne puisse jamais diverger du statut de la commande : un depart, une livraison, une annulation ou un
echec mettent a jour la livraison vivante au meme instant.

Ce module n'importe que les modeles (pas de dependance vers ``orders.service``) pour eviter un
import circulaire.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.delivery import estimates
from app.modules.delivery.models import (
    DELIVERY_ACTIVE_STATUSES,
    Delivery,
    DeliveryEvent,
    DeliveryRun,
    DeliveryZone,
    RestaurantDeliverySettings,
)
from app.modules.hr.models import Establishment


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def is_dispatch_enabled(session: AsyncSession) -> bool:
    """Lecture seule : sans ligne de reglages, le dispatch par livreurs est coupe."""
    value = await session.scalar(
        select(RestaurantDeliverySettings.driver_dispatch_enabled)
        .order_by(RestaurantDeliverySettings.id)
        .limit(1)
    )
    return bool(value)


async def active_delivery(session: AsyncSession, order_id: int, *, lock: bool = False) -> Delivery | None:
    stmt = select(Delivery).where(
        Delivery.order_id == order_id, Delivery.status.in_(DELIVERY_ACTIVE_STATUSES)
    )
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


def add_event(
    session: AsyncSession,
    delivery: Delivery | None,
    *,
    order_id: int,
    event: str,
    actor_user_id: int | None,
    driver_id: int | None = None,
    note: str | None = None,
) -> None:
    session.add(
        DeliveryEvent(
            delivery_id=delivery.id if delivery is not None else None,
            order_id=order_id,
            driver_id=driver_id if driver_id is not None else (delivery.driver_id if delivery else None),
            event=event,
            actor_user_id=actor_user_id,
            note=(note or None) and note[:256],
        )
    )


async def ensure_driver_for_departure(session: AsyncSession, order) -> None:
    """``ready -> out_for_delivery`` d'une livraison exige un livreur assigne quand le dispatch est
    actif. Appele avant toute modification de la commande."""
    if order.order_type != "delivery":
        return
    if not await is_dispatch_enabled(session):
        return
    delivery = await active_delivery(session, order.id)
    if delivery is None:
        raise AppError(
            "DRIVER_REQUIRED",
            "Assignez un livreur a cette commande avant le depart.",
            409,
            "status",
        )


async def _refresh_estimate(session: AsyncSession, order, now: datetime, other_stops: int) -> None:
    """Recalcule l'heure de remise estimee au depart (trajet depuis le restaurant, arrets de la tournee).
    Sans position connue ni zone, l'estimation faite a la commande est conservee."""
    establishment = await session.get(Establishment, order.establishment_id) if order.establishment_id else None
    zone = await session.get(DeliveryZone, order.delivery_zone_id) if order.delivery_zone_id else None
    minutes = estimates.departure_minutes(
        origin=(
            (establishment.latitude, establishment.longitude) if establishment is not None else (None, None)
        ),
        destination=(order.delivery_lat, order.delivery_lng),
        zone_minutes=zone.estimated_minutes if zone is not None else None,
        other_stops=other_stops,
    )
    if minutes is not None:
        order.estimated_delivery_at = now + timedelta(minutes=minutes)


async def on_departure(session: AsyncSession, order, actor_user_id: int | None) -> None:
    """La commande part en livraison : la livraison assignee passe ``out_for_delivery`` et rejoint
    la tournee en cours du livreur (ou en ouvre une). L'heure de remise estimee est recalculee."""
    if order.order_type != "delivery":
        return
    delivery = await active_delivery(session, order.id, lock=True)
    if delivery is None:
        # Dispatch coupe et aucun livreur : comportement historique, mais l'estimation reste a jour.
        await _refresh_estimate(session, order, _now(), 0)
        return
    now = _now()
    run = await session.scalar(
        select(DeliveryRun)
        .where(DeliveryRun.driver_id == delivery.driver_id, DeliveryRun.status == "active")
        .order_by(DeliveryRun.id.desc())
        .limit(1)
    )
    if run is None:
        run = DeliveryRun(driver_id=delivery.driver_id, status="active", started_at=now)
        session.add(run)
        await session.flush()
    others = await session.scalar(
        select(func.count(Delivery.id)).where(
            Delivery.run_id == run.id,
            Delivery.id != delivery.id,
            Delivery.status.in_(DELIVERY_ACTIVE_STATUSES),
        )
    )
    await _refresh_estimate(session, order, now, int(others or 0))
    delivery.status = "out_for_delivery"
    delivery.departed_at = now
    delivery.run_id = run.id
    add_event(session, delivery, order_id=order.id, event="departed", actor_user_id=actor_user_id)


async def _complete_run_if_done(session: AsyncSession, run_id: int | None) -> None:
    if run_id is None:
        return
    remaining = await session.scalar(
        select(func.count(Delivery.id)).where(
            Delivery.run_id == run_id, Delivery.status.in_(DELIVERY_ACTIVE_STATUSES)
        )
    )
    if not remaining:
        run = await session.get(DeliveryRun, run_id)
        if run is not None and run.status == "active":
            run.status = "completed"
            run.ended_at = _now()


async def proof_required_for(session: AsyncSession, order) -> bool:
    """La preuve de remise s'impose quand elle est activee ET que la commande a une livraison
    vivante (donc un livreur) : une livraison sans livreur reste regie par le comportement historique."""
    if order.order_type != "delivery":
        return False
    required = await session.scalar(
        select(RestaurantDeliverySettings.delivery_proof_required)
        .order_by(RestaurantDeliverySettings.id)
        .limit(1)
    )
    if not required:
        return False
    return await active_delivery(session, order.id) is not None


async def on_delivered(
    session: AsyncSession, order, actor_user_id: int | None, proof: str | None = None
) -> None:
    delivery = await active_delivery(session, order.id, lock=True)
    if delivery is None:
        return
    delivery.status = "delivered"
    delivery.finished_at = _now()
    add_event(
        session,
        delivery,
        order_id=order.id,
        event="delivered",
        actor_user_id=actor_user_id,
        note=f"proof:{proof}" if proof else None,
    )
    await session.flush()
    await _complete_run_if_done(session, delivery.run_id)


async def on_order_closed(session: AsyncSession, order, new_status: str, actor_user_id: int | None) -> None:
    """Annulation, rejet ou echec de livraison : la livraison vivante est cloturee."""
    delivery = await active_delivery(session, order.id, lock=True)
    if delivery is None:
        return
    delivery.status = "failed" if new_status == "delivery_failed" else "cancelled"
    delivery.finished_at = _now()
    add_event(
        session,
        delivery,
        order_id=order.id,
        event="failed" if new_status == "delivery_failed" else "cancelled",
        actor_user_id=actor_user_id,
        note=f"order_{new_status}",
    )
    await session.flush()
    await _complete_run_if_done(session, delivery.run_id)
