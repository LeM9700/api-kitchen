"""Livreurs et dispatch comptoir.

Le livreur est un employe du restaurant : un ``users`` de role ``driver`` (aucun acces hors
pointage et livraisons : toutes les autres routes refusent ce role par defaut) plus un
``EmployeeProfile`` (pointage) et un ``DriverProfile`` (rattachement a un etablissement).

Les transitions du statut de commande restent dans ``orders.update_status`` ; les fonctions
livreur d'ici verifient les droits, puis l'appellent. La table ``deliveries`` suit la commande via
``delivery/lifecycle.py``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.security import get_password_hash
from app.core.http.errors import AppError
from app.core.i18n.translate import t
from app.modules.auth.models import User
from app.modules.delivery import lifecycle
from app.modules.delivery.models import (
    DELIVERY_ACTIVE_STATUSES,
    Delivery,
    DeliveryRun,
    DriverProfile,
)
from app.modules.hr.models import Establishment, EmployeeProfile, TimeClockEntry
from app.modules.notifications.notification_service import notify_user
from app.modules.orders.models import Order, OrderItem
from app.modules.payments.models import Payment

logger = logging.getLogger(__name__)

# Une commande n'est attribuable qu'une fois confirmee (donc payee ou garantie) et tant qu'elle
# n'est pas partie.
ASSIGNABLE_ORDER_STATUSES = ("confirmed", "queued", "preparing", "ready")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- livreurs


async def _presence(session: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    """``user_id -> 'open' | 'break'`` pour les livreurs actuellement pointes."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(EmployeeProfile.user_id, TimeClockEntry.status)
        .join(TimeClockEntry, TimeClockEntry.employee_id == EmployeeProfile.id)
        .where(EmployeeProfile.user_id.in_(user_ids), TimeClockEntry.status.in_(("open", "break")))
    )
    return {user_id: status for user_id, status in rows.all()}


def _day_bounds(tz_name: str | None, day: date | None = None) -> tuple[datetime, datetime, date]:
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local_day = day or datetime.now(tz).date()
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc), local_day


async def _driver_out(
    session: AsyncSession, drivers: list[DriverProfile]
) -> list[dict]:
    if not drivers:
        return []
    user_ids = [d.user_id for d in drivers]
    users = {
        u.id: u
        for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
    }
    presence = await _presence(session, user_ids)
    active_counts = dict(
        (
            await session.execute(
                select(Delivery.driver_id, func.count(Delivery.id))
                .where(
                    Delivery.driver_id.in_([d.id for d in drivers]),
                    Delivery.status.in_(DELIVERY_ACTIVE_STATUSES),
                )
                .group_by(Delivery.driver_id)
            )
        ).all()
    )
    timezones = {
        e.id: e.timezone
        for e in (
            await session.execute(
                select(Establishment).where(Establishment.id.in_({d.establishment_id for d in drivers}))
            )
        ).scalars()
    }
    out: list[dict] = []
    for d in drivers:
        start, end, _ = _day_bounds(timezones.get(d.establishment_id))
        delivered_today = await session.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.driver_id == d.id,
                Delivery.status == "delivered",
                Delivery.finished_at >= start,
                Delivery.finished_at < end,
            )
        )
        user = users.get(d.user_id)
        status = presence.get(d.user_id)
        out.append(
            {
                "id": d.id,
                "user_id": d.user_id,
                "full_name": user.full_name if user else None,
                "email": user.email if user else None,
                "phone": d.phone,
                "vehicle": d.vehicle,
                "establishment_id": d.establishment_id,
                "is_active": d.is_active,
                "clocked_in": status is not None,
                "on_break": status == "break",
                "active_deliveries": int(active_counts.get(d.id, 0)),
                "delivered_today": int(delivered_today or 0),
            }
        )
    return out


async def list_drivers(session: AsyncSession, establishment_id: int | None = None) -> list[dict]:
    stmt = select(DriverProfile).order_by(DriverProfile.is_active.desc(), DriverProfile.id)
    if establishment_id is not None:
        stmt = stmt.where(DriverProfile.establishment_id == establishment_id)
    return await _driver_out(session, list((await session.execute(stmt)).scalars()))


async def create_driver(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    phone: str | None,
    vehicle: str | None,
    establishment_id: int,
) -> dict:
    """Cree le compte (role ``driver``, mot de passe temporaire a changer), le profil employe (pointage)
    et le profil livreur, en une seule transaction."""
    establishment = await session.get(Establishment, establishment_id)
    if establishment is None or not establishment.is_active:
        raise AppError("ESTABLISHMENT_NOT_FOUND", "Etablissement introuvable ou inactif", 404, "establishment_id")
    email = email.strip().lower()
    if await session.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise AppError("EMAIL_EXISTS", "Email already registered", 409, "email")

    temp_password = secrets.token_urlsafe(12)
    user = User(
        email=email,
        full_name=full_name,
        password_hash=get_password_hash(temp_password),
        role="driver",
        # Aucun droit fin : le role driver n'ouvre que les routes qui le nomment explicitement.
        permissions=[],
        must_change_password=True,
        email_verified_at=_now(),
    )
    session.add(user)
    await session.flush()
    session.add(EmployeeProfile(user_id=user.id, establishment_id=establishment_id))
    driver = DriverProfile(
        user_id=user.id,
        establishment_id=establishment_id,
        phone=(phone or "").strip() or None,
        vehicle=(vehicle or "").strip() or None,
    )
    session.add(driver)
    await session.commit()
    await session.refresh(driver)
    (out,) = await _driver_out(session, [driver])
    return {**out, "temporary_password": temp_password}


async def update_driver(session: AsyncSession, driver_id: int, fields: dict) -> dict:
    driver = await session.get(DriverProfile, driver_id, with_for_update=True)
    if driver is None:
        raise AppError("DRIVER_NOT_FOUND", "Livreur introuvable", 404)
    if fields.get("is_active") is False and driver.is_active:
        busy = await session.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.driver_id == driver.id, Delivery.status.in_(DELIVERY_ACTIVE_STATUSES)
            )
        )
        if busy:
            raise AppError(
                "DRIVER_HAS_ACTIVE_DELIVERIES",
                "Ce livreur a des livraisons en cours : reattribuez-les d'abord.",
                409,
            )
    for key in ("is_active", "phone", "vehicle"):
        if key in fields:
            value = fields[key]
            if isinstance(value, str):
                value = value.strip() or None
            setattr(driver, key, value)
    await session.commit()
    await session.refresh(driver)
    (out,) = await _driver_out(session, [driver])
    return out


# --------------------------------------------------------------------------- tableau de dispatch


async def _items_counts(session: AsyncSession, order_ids: list[int]) -> dict[int, int]:
    if not order_ids:
        return {}
    rows = await session.execute(
        select(OrderItem.order_id, func.coalesce(func.sum(OrderItem.quantity), 0))
        .where(OrderItem.order_id.in_(order_ids))
        .group_by(OrderItem.order_id)
    )
    return {order_id: int(count) for order_id, count in rows.all()}


def _order_dict(order: Order, items_count: int) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "establishment_id": order.establishment_id,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_address": order.delivery_address,
        "delivery_lat": order.delivery_lat,
        "delivery_lng": order.delivery_lng,
        "delivery_instructions": order.delivery_instructions,
        "total": float(order.total),
        "payment_status": order.payment_status,
        "items_count": items_count,
        "created_at": order.created_at,
        "estimated_delivery_at": order.estimated_delivery_at,
    }


async def dispatch_board(session: AsyncSession, establishment_id: int | None = None) -> dict:
    has_active = exists().where(
        Delivery.order_id == Order.id, Delivery.status.in_(DELIVERY_ACTIVE_STATUSES)
    )
    stmt = (
        select(Order)
        .where(
            Order.order_type == "delivery",
            Order.status.in_(ASSIGNABLE_ORDER_STATUSES),
            ~has_active,
        )
        .order_by(Order.created_at, Order.id)
    )
    if establishment_id is not None:
        stmt = stmt.where(Order.establishment_id == establishment_id)
    unassigned = list((await session.execute(stmt)).scalars())

    dstmt = (
        select(Delivery, Order, User.full_name)
        .join(Order, Order.id == Delivery.order_id)
        .join(DriverProfile, DriverProfile.id == Delivery.driver_id)
        .join(User, User.id == DriverProfile.user_id)
        .where(Delivery.status.in_(DELIVERY_ACTIVE_STATUSES))
        .order_by(Delivery.assigned_at, Delivery.id)
    )
    if establishment_id is not None:
        dstmt = dstmt.where(DriverProfile.establishment_id == establishment_id)
    rows = (await session.execute(dstmt)).all()

    counts = await _items_counts(session, [o.id for o in unassigned] + [r[1].id for r in rows])
    return {
        "dispatch_enabled": await lifecycle.is_dispatch_enabled(session),
        "unassigned": [_order_dict(o, counts.get(o.id, 0)) for o in unassigned],
        "deliveries": [
            {
                "id": delivery.id,
                "status": delivery.status,
                "driver_id": delivery.driver_id,
                "driver_name": name,
                "run_id": delivery.run_id,
                "assigned_at": delivery.assigned_at,
                "departed_at": delivery.departed_at,
                "arrived_at": delivery.arrived_at,
                "order": _order_dict(order, counts.get(order.id, 0)),
            }
            for delivery, order, name in rows
        ],
        "drivers": await list_drivers(session, establishment_id),
    }


# --------------------------------------------------------------------------- attribution


async def assign(
    session: AsyncSession,
    *,
    order_ids: list[int],
    driver_id: int,
    actor_user_id: int,
    tenant_slug: str,
) -> list[Delivery]:
    """Attribue une ou plusieurs commandes a un livreur (ou les reattribue tant qu'elles ne sont
    pas parties). Tout ou rien : si une commande est refusee, aucune n'est attribuee."""
    driver = await session.get(DriverProfile, driver_id)
    if driver is None:
        raise AppError("DRIVER_NOT_FOUND", "Livreur introuvable", 404, "driver_id")
    if not driver.is_active:
        raise AppError("DRIVER_INACTIVE", "Ce livreur est desactive.", 409, "driver_id")
    if driver.user_id not in await _presence(session, [driver.user_id]):
        raise AppError(
            "DRIVER_NOT_CLOCKED_IN",
            "Ce livreur n'a pas pointe : il ne peut pas recevoir de livraison.",
            409,
            "driver_id",
        )

    # Verrou dans un ordre stable pour que deux attributions simultanees ne s'interbloquent pas.
    orders = {
        o.id: o
        for o in (
            await session.execute(
                select(Order).where(Order.id.in_(order_ids)).order_by(Order.id).with_for_update()
            )
        ).scalars()
    }
    missing = [oid for oid in order_ids if oid not in orders]
    if missing:
        raise AppError("ORDER_NOT_FOUND", f"Commande introuvable : #{missing[0]}", 404, "order_ids")

    result: list[Delivery] = []
    changed: list[Delivery] = []
    for order_id in sorted(order_ids):
        order = orders[order_id]
        if order.order_type != "delivery":
            raise AppError("ORDER_NOT_DELIVERY", f"La commande #{order_id} n'est pas une livraison.", 422, "order_ids")
        current = await lifecycle.active_delivery(session, order_id, lock=True)
        if current is not None and current.status != "assigned":
            raise AppError(
                "DELIVERY_ALREADY_STARTED",
                f"La commande #{order_id} est deja partie en livraison.",
                409,
                "order_ids",
            )
        if order.status not in ASSIGNABLE_ORDER_STATUSES:
            raise AppError(
                "ORDER_NOT_ASSIGNABLE",
                f"La commande #{order_id} ne peut pas etre attribuee dans son etat actuel.",
                409,
                "order_ids",
            )
        if order.establishment_id is not None and order.establishment_id != driver.establishment_id:
            raise AppError(
                "DRIVER_WRONG_ESTABLISHMENT",
                f"La commande #{order_id} appartient a un autre etablissement que ce livreur.",
                409,
                "order_ids",
            )
        if current is None:
            delivery = Delivery(
                order_id=order_id,
                driver_id=driver.id,
                status="assigned",
                assigned_by_user_id=actor_user_id,
                assigned_at=_now(),
            )
            session.add(delivery)
            await session.flush()
            lifecycle.add_event(
                session, delivery, order_id=order_id, event="assigned", actor_user_id=actor_user_id
            )
            changed.append(delivery)
            result.append(delivery)
        elif current.driver_id == driver.id:
            result.append(current)  # deja attribuee a ce livreur : rien a faire
        else:
            previous = current.driver_id
            current.driver_id = driver.id
            current.assigned_by_user_id = actor_user_id
            current.assigned_at = _now()
            lifecycle.add_event(
                session,
                current,
                order_id=order_id,
                event="reassigned",
                actor_user_id=actor_user_id,
                note=f"from_driver_{previous}",
            )
            changed.append(current)
            result.append(current)
    await session.commit()

    if changed:
        await _notify_driver_assigned(session, tenant_slug, driver, [d.order_id for d in changed])
    return result


async def _notify_driver_assigned(
    session: AsyncSession, tenant_slug: str, driver: DriverProfile, order_ids: list[int]
) -> None:
    try:
        label = f"#{order_ids[0]}" if len(order_ids) == 1 else f"{len(order_ids)} commandes"
        await notify_user(
            session=session,
            tenant_slug=tenant_slug,
            user_id=driver.user_id,
            event="delivery.assigned",
            title="Nouvelle livraison",
            body=f"Livraison {label} attribuee.",
            data={"order_ids": order_ids},
        )
    except Exception as exc:  # une notification ne doit jamais faire echouer l'attribution
        logger.warning("driver assignment notification failed: %s", exc)


async def unassign(session: AsyncSession, *, order_id: int, actor_user_id: int) -> Delivery:
    order = await session.get(Order, order_id, with_for_update=True)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Commande introuvable", 404)
    delivery = await lifecycle.active_delivery(session, order_id, lock=True)
    if delivery is None:
        raise AppError("DELIVERY_NOT_FOUND", "Cette commande n'a pas de livreur assigne.", 404)
    if delivery.status != "assigned":
        raise AppError(
            "DELIVERY_ALREADY_STARTED", "Le livreur est deja parti avec cette commande.", 409
        )
    delivery.status = "cancelled"
    delivery.finished_at = _now()
    lifecycle.add_event(session, delivery, order_id=order_id, event="unassigned", actor_user_id=actor_user_id)
    await session.commit()
    await session.refresh(delivery)
    return delivery


# --------------------------------------------------------------------------- cote livreur


async def get_driver_for_user(session: AsyncSession, user_id: int) -> DriverProfile:
    driver = await session.scalar(select(DriverProfile).where(DriverProfile.user_id == user_id))
    if driver is None:
        raise AppError("DRIVER_PROFILE_REQUIRED", "Aucun profil livreur pour ce compte.", 403)
    if not driver.is_active:
        raise AppError("DRIVER_INACTIVE", "Votre compte livreur est desactive.", 403)
    return driver


async def driver_me(session: AsyncSession, driver: DriverProfile) -> dict:
    (out,) = await _driver_out(session, [driver])
    return {
        "id": driver.id,
        "user_id": driver.user_id,
        "full_name": out["full_name"],
        "phone": driver.phone,
        "vehicle": driver.vehicle,
        "establishment_id": driver.establishment_id,
        "clocked_in": out["clocked_in"],
        "on_break": out["on_break"],
    }


def _amount_due(order: Order) -> float:
    # A regler a la remise : uniquement une empreinte bancaire (le client paie en especes).
    return float(order.total) if order.payment_status == "guaranteed" else 0.0


def _phase(delivery: Delivery, order: Order) -> str:
    if delivery.status == "arrived":
        return "arrived"
    if delivery.status == "out_for_delivery":
        return "en_route"
    return "ready" if order.status == "ready" else "upcoming"


async def driver_deliveries(session: AsyncSession, driver: DriverProfile) -> list[dict]:
    rows = (
        await session.execute(
            select(Delivery, Order)
            .join(Order, Order.id == Delivery.order_id)
            .where(Delivery.driver_id == driver.id, Delivery.status.in_(DELIVERY_ACTIVE_STATUSES))
            .order_by(Delivery.assigned_at, Delivery.id)
        )
    ).all()
    counts = await _items_counts(session, [order.id for _, order in rows])
    return [
        {
            "id": delivery.id,
            "order_id": order.id,
            "status": delivery.status,
            "phase": _phase(delivery, order),
            "run_id": delivery.run_id,
            "customer_name": order.customer_name,
            "customer_phone": order.customer_phone,
            "delivery_address": order.delivery_address,
            "delivery_lat": order.delivery_lat,
            "delivery_lng": order.delivery_lng,
            "delivery_instructions": order.delivery_instructions,
            "items_count": counts.get(order.id, 0),
            "total": float(order.total),
            "amount_due": _amount_due(order),
            "payment_status": order.payment_status,
            "estimated_delivery_at": order.estimated_delivery_at,
            "assigned_at": delivery.assigned_at,
            "departed_at": delivery.departed_at,
        }
        for delivery, order in rows
    ]


async def _own_delivery(session: AsyncSession, driver: DriverProfile, delivery_id: int) -> Delivery:
    delivery = await session.get(Delivery, delivery_id, with_for_update=True)
    # 404 (et non 403) pour la livraison d'un autre livreur : on ne revele pas qu'elle existe.
    if delivery is None or delivery.driver_id != driver.id:
        raise AppError("DELIVERY_NOT_FOUND", "Livraison introuvable", 404)
    return delivery


async def driver_depart(
    session: AsyncSession,
    driver: DriverProfile,
    delivery_ids: list[int],
    *,
    user_id: int,
    tenant_slug: str,
    arq_pool=None,
) -> list[Delivery]:
    from app.modules.orders import service as orders_service

    deliveries: list[Delivery] = []
    for delivery_id in sorted(delivery_ids):
        delivery = await _own_delivery(session, driver, delivery_id)
        if delivery.status != "assigned":
            raise AppError(
                "DELIVERY_NOT_ASSIGNED_STATE",
                f"La livraison #{delivery_id} n'est pas a l'etat « a venir ».",
                409,
            )
        order = await session.get(Order, delivery.order_id)
        if order is None or order.status != "ready":
            raise AppError(
                "ORDER_NOT_READY",
                f"La commande #{delivery.order_id} n'est pas encore prete.",
                409,
            )
        deliveries.append(delivery)
    order_ids = [d.order_id for d in deliveries]
    delivery_pks = [d.id for d in deliveries]
    await session.rollback()  # libere les verrous : update_status reprend ceux de chaque commande
    for order_id in order_ids:
        await orders_service.update_status(
            session,
            order_id,
            "out_for_delivery",
            tenant_slug=tenant_slug,
            arq_pool=arq_pool,
            actor_user_id=user_id,
            is_staff=True,
        )
    return list(
        (await session.execute(select(Delivery).where(Delivery.id.in_(delivery_pks)))).scalars()
    )


async def driver_arrived(
    session: AsyncSession, driver: DriverProfile, delivery_id: int, *, user_id: int, tenant_slug: str
) -> Delivery:
    delivery = await _own_delivery(session, driver, delivery_id)
    if delivery.status == "arrived":
        return delivery  # idempotent
    if delivery.status != "out_for_delivery":
        raise AppError("DELIVERY_NOT_EN_ROUTE", "Cette livraison n'est pas en route.", 409)
    delivery.status = "arrived"
    delivery.arrived_at = _now()
    lifecycle.add_event(session, delivery, order_id=delivery.order_id, event="arrived", actor_user_id=user_id)
    order = await session.get(Order, delivery.order_id)
    customer_id = order.user_id if order is not None else None
    order_id = delivery.order_id
    await session.commit()
    await session.refresh(delivery)
    if customer_id is not None:
        try:
            await notify_user(
                session=session,
                tenant_slug=tenant_slug,
                user_id=customer_id,
                event="order.driver_arrived",
                title=t("Your driver has arrived"),
                body=t("Your driver is at your address with order #{order_id}.", order_id=order_id),
                data={"order_id": order_id},
            )
        except Exception as exc:
            logger.warning("driver arrived notification failed: %s", exc)
    return delivery


async def driver_deliver(
    session: AsyncSession,
    driver: DriverProfile,
    delivery_id: int,
    *,
    cash_received: float | None,
    user_id: int,
    tenant_slug: str,
    arq_pool=None,
) -> Delivery:
    from app.modules.orders import service as orders_service
    from app.modules.payments import guarantee

    delivery = await _own_delivery(session, driver, delivery_id)
    if delivery.status not in ("out_for_delivery", "arrived"):
        raise AppError("DELIVERY_NOT_EN_ROUTE", "Cette livraison n'est pas en route.", 409)
    order = await session.get(Order, delivery.order_id, with_for_update=True, populate_existing=True)
    order_id = delivery.order_id
    payment_status = order.payment_status
    total = float(order.total)
    await session.rollback()  # libere les verrous avant le reglement et la livraison

    if payment_status == "guaranteed":
        if cash_received is None:
            raise AppError(
                "CASH_RECEIVED_REQUIRED",
                f"Saisissez les especes recues ({total:.2f}) avant de valider la livraison.",
                422,
                "cash_received",
            )
        await guarantee.settle_guarantee_cash(
            session, tenant_slug, order_id, amount_received=cash_received, user_id=user_id
        )
        payment_status = "paid"
    if payment_status != "paid":
        # Libere par un admin sans debit, debite en partie, etc. : le livreur ne conclut pas seul.
        raise AppError(
            "ORDER_NOT_PAID",
            "Le reglement de cette commande n'est pas enregistre : contactez le restaurant.",
            409,
        )
    await orders_service.update_status(
        session,
        order_id,
        "delivered",
        tenant_slug=tenant_slug,
        arq_pool=arq_pool,
        actor_user_id=user_id,
        is_staff=True,
    )
    refreshed = await session.get(Delivery, delivery_id, populate_existing=True)
    return refreshed


async def driver_recap(session: AsyncSession, driver: DriverProfile, day: date | None = None) -> dict:
    establishment = await session.get(Establishment, driver.establishment_id)
    start, end, local_day = _day_bounds(establishment.timezone if establishment else None, day)
    rows = (
        await session.execute(
            select(Delivery, Order)
            .join(Order, Order.id == Delivery.order_id)
            .where(
                Delivery.driver_id == driver.id,
                Delivery.status == "delivered",
                Delivery.finished_at >= start,
                Delivery.finished_at < end,
            )
            .order_by(Delivery.finished_at)
        )
    ).all()
    runs = await session.scalar(
        select(func.count(DeliveryRun.id)).where(
            DeliveryRun.driver_id == driver.id, DeliveryRun.started_at >= start, DeliveryRun.started_at < end
        )
    )
    cash = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            and_(
                Payment.provider == "cash",
                Payment.purpose == "sale",
                Payment.settled_by_user_id == driver.user_id,
                Payment.settled_at >= start,
                Payment.settled_at < end,
            )
        )
    )
    return {
        "day": local_day,
        "delivered_count": len(rows),
        "runs_count": int(runs or 0),
        "cash_collected": float(cash or 0),
        "deliveries": [
            {
                "delivery_id": delivery.id,
                "order_id": order.id,
                "delivery_address": order.delivery_address,
                "total": float(order.total),
                "finished_at": delivery.finished_at,
            }
            for delivery, order in rows
        ],
    }
