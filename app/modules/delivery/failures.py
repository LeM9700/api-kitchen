"""Echecs de livraison : declaration par le livreur, traitement par un administrateur.

Regles (reglables par etablissement dans ``restaurant_delivery_settings``) :
- « client absent » et « client injoignable » exigent que le livreur ait signale son arrivee,
  attendu ``failure_min_wait_minutes`` et appele au moins ``failure_min_call_attempts`` fois ;
- un echec « probleme de commande » est de la **faute du restaurant** : aucun frais ne peut etre
  retenu au client.

La declaration et le passage de la commande a ``delivery_failed`` se font dans la meme transaction :
il n'existe pas d'echec sans enregistrement a traiter, ni d'enregistrement sans echec.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.delivery import lifecycle, proof
from app.modules.delivery.models import (
    FAILURE_REASONS,
    Delivery,
    DeliveryFailure,
    DriverProfile,
    RestaurantDeliverySettings,
)
from app.modules.auth.models import User
from app.modules.orders.models import Order

WAITING_REASONS = {"customer_absent", "customer_unreachable"}
REASON_LABELS = {
    "customer_absent": "Client absent",
    "customer_unreachable": "Client injoignable",
    "wrong_address": "Adresse introuvable ou fausse",
    "customer_refused": "Client a refuse la commande",
    "order_problem": "Probleme de commande (restaurant)",
    "other": "Autre",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_rules(session: AsyncSession) -> dict:
    row = await session.scalar(select(RestaurantDeliverySettings).order_by(RestaurantDeliverySettings.id).limit(1))
    return {
        "proof_required": bool(row.delivery_proof_required) if row else False,
        "min_wait_minutes": int(row.failure_min_wait_minutes) if row else 5,
        "min_call_attempts": int(row.failure_min_call_attempts) if row else 1,
    }


async def report_failure(
    session: AsyncSession,
    driver: DriverProfile,
    delivery_id: int,
    *,
    reason: str,
    note: str | None,
    call_attempts: int,
    user_id: int,
    tenant_slug: str,
    arq_pool=None,
) -> DeliveryFailure:
    from app.modules.delivery.dispatch_service import _own_delivery
    from app.modules.orders import service as orders_service

    if reason not in FAILURE_REASONS:
        raise AppError("FAILURE_REASON_INVALID", "Motif d'echec inconnu.", 422, "reason")
    note = (note or "").strip() or None
    if reason == "other" and (note is None or len(note) < 3):
        raise AppError("FAILURE_NOTE_REQUIRED", "Precisez le motif de l'echec.", 422, "note")

    delivery = await _own_delivery(session, driver, delivery_id)
    if delivery.status not in ("out_for_delivery", "arrived"):
        raise AppError("DELIVERY_NOT_EN_ROUTE", "Cette livraison n'est pas en route.", 409)

    waited: int | None = None
    if reason in WAITING_REASONS:
        rules = await get_rules(session)
        if delivery.status != "arrived" or delivery.arrived_at is None:
            raise AppError(
                "FAILURE_ARRIVAL_REQUIRED",
                "Signalez votre arrivee avant de declarer le client absent ou injoignable.",
                409,
            )
        arrived_at = delivery.arrived_at
        if arrived_at.tzinfo is None:
            arrived_at = arrived_at.replace(tzinfo=timezone.utc)
        waited = int((_now() - arrived_at).total_seconds())
        needed = rules["min_wait_minutes"] * 60
        if waited < needed:
            remaining = needed - waited
            raise AppError(
                "FAILURE_TOO_EARLY",
                f"Attendez encore {(remaining + 59) // 60} minute(s) avant de declarer l'echec.",
                409,
            )
        if call_attempts < rules["min_call_attempts"]:
            raise AppError(
                "FAILURE_CALL_REQUIRED",
                f"Appelez le client au moins {rules['min_call_attempts']} fois avant de declarer l'echec.",
                422,
                "call_attempts",
            )

    failure = DeliveryFailure(
        delivery_id=delivery.id,
        order_id=delivery.order_id,
        driver_id=driver.id,
        reason=reason,
        fault=FAILURE_REASONS[reason],
        note=note[:256] if note else None,
        call_attempts=max(0, int(call_attempts)),
        waited_seconds=waited,
    )
    session.add(failure)
    await session.flush()
    order_id = delivery.order_id
    label = REASON_LABELS[reason] + (f" - {note}" if note else "")
    # Commit unique : echec enregistre + commande « delivery_failed » + livraison cloturee.
    await orders_service.update_status(
        session,
        order_id,
        "delivery_failed",
        label,
        tenant_slug=tenant_slug,
        arq_pool=arq_pool,
        actor_user_id=user_id,
        is_staff=True,
    )
    await session.refresh(failure)
    return failure


async def list_failures(session: AsyncSession, status: str | None = "pending") -> list[dict]:
    stmt = (
        select(DeliveryFailure, Order, User.full_name)
        .join(Order, Order.id == DeliveryFailure.order_id)
        .join(DriverProfile, DriverProfile.id == DeliveryFailure.driver_id)
        .join(User, User.id == DriverProfile.user_id)
        .order_by(DeliveryFailure.created_at.desc(), DeliveryFailure.id.desc())
        .limit(200)
    )
    if status:
        stmt = stmt.where(DeliveryFailure.status == status)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": f.id,
            "order_id": o.id,
            "delivery_id": f.delivery_id,
            "driver_id": f.driver_id,
            "driver_name": name,
            "reason": f.reason,
            "reason_label": REASON_LABELS.get(f.reason, f.reason),
            "fault": f.fault,
            "note": f.note,
            "call_attempts": f.call_attempts,
            "waited_seconds": f.waited_seconds,
            "status": f.status,
            "resolution": f.resolution,
            "retained_amount": float(f.retained_amount) if f.retained_amount is not None else None,
            "created_at": f.created_at,
            "resolved_at": f.resolved_at,
            "customer_name": o.customer_name,
            "customer_phone": o.customer_phone,
            "delivery_address": o.delivery_address,
            "total": float(o.total),
            "payment_status": o.payment_status,
        }
        for f, o, name in rows
    ]


# Deja fait : un rejeu apres un succes partiel ne doit ni echouer ni rembourser deux fois.
_ALREADY_DONE = {"REFUND_ALREADY_COMPLETE", "GUARANTEE_NOT_FOUND"}


async def resolve_failure(
    session: AsyncSession,
    failure_id: int,
    *,
    action: str,
    amount_cents: int | None,
    note: str | None,
    admin_user_id: int,
    tenant_slug: str,
    arq_pool=None,
) -> DeliveryFailure:
    from app.modules.orders import service as orders_service
    from app.modules.payments import guarantee
    from app.modules.payments import service as payments_service

    note = (note or "").strip() or None
    if action not in {"refund", "retain", "redeliver"}:
        raise AppError("RESOLUTION_INVALID", "Action inconnue.", 422, "action")

    failure = await session.get(DeliveryFailure, failure_id, with_for_update=True, populate_existing=True)
    if failure is None:
        raise AppError("FAILURE_NOT_FOUND", "Echec introuvable.", 404)
    if failure.status != "pending":
        raise AppError("FAILURE_ALREADY_RESOLVED", "Cet echec a deja ete traite.", 409)
    order = await session.get(Order, failure.order_id, with_for_update=True, populate_existing=True)
    order_id, total_cents = order.id, int(round(float(order.total) * 100))
    payment_status = order.payment_status
    fault = failure.fault
    retained: float | None = None
    reason = (note or f"delivery_failed_{action}")[:256]
    await session.rollback()  # libere les verrous : les services appeles reprennent les leurs

    try:
        if action == "refund":
            if payment_status in {"paid", "guarantee_captured"}:
                await payments_service.create_refund(
                    session, tenant_slug, order_id, admin_user_id, None, reason, allow_unfulfilled_order=True
                )
            elif payment_status == "guaranteed":
                await guarantee.release_guarantee(
                    session, tenant_slug, order_id, reason=reason, user_id=admin_user_id
                )
        elif action == "retain":
            if fault == "restaurant":
                raise AppError(
                    "RETAIN_NOT_ALLOWED",
                    "L'echec est de la faute du restaurant : aucun frais ne peut etre retenu.",
                    409,
                    "action",
                )
            if amount_cents is None or amount_cents <= 0 or amount_cents > total_cents:
                raise AppError(
                    "RETAIN_AMOUNT_INVALID",
                    "Le montant retenu doit etre positif et ne pas depasser le total de la commande.",
                    422,
                    "amount",
                )
            if not note:
                raise AppError("RESOLUTION_NOTE_REQUIRED", "Un motif est requis pour retenir des frais.", 422, "note")
            if payment_status == "guaranteed":
                await guarantee.capture_guarantee(
                    session, tenant_slug, order_id, amount_cents=amount_cents, reason=note, user_id=admin_user_id
                )
            elif payment_status in {"paid", "guarantee_captured"}:
                if amount_cents < total_cents:
                    await payments_service.create_refund(
                        session,
                        tenant_slug,
                        order_id,
                        admin_user_id,
                        total_cents - amount_cents,
                        reason,
                        allow_unfulfilled_order=True,
                    )
            else:
                raise AppError("RETAIN_NOT_POSSIBLE", "Aucun paiement a retenir pour cette commande.", 409)
            retained = amount_cents / 100
        else:  # redeliver
            if payment_status not in {"paid", "guaranteed"}:
                raise AppError(
                    "REDELIVERY_NOT_POSSIBLE",
                    "Cette commande n'est plus reglee : elle ne peut pas etre relivree.",
                    409,
                )
            await proof.regenerate_nonce(session, order_id)  # nouveau code, l'ancien ne vaut plus rien
            await orders_service.update_status(
                session,
                order_id,
                "ready",
                note or "Relivraison",
                tenant_slug=tenant_slug,
                arq_pool=arq_pool,
                actor_user_id=admin_user_id,
                is_staff=True,
                allow_redelivery=True,
            )
    except AppError as exc:
        if exc.code not in _ALREADY_DONE:
            await session.rollback()
            raise
        await session.rollback()

    failure = await session.get(DeliveryFailure, failure_id, with_for_update=True, populate_existing=True)
    failure.status = "resolved"
    failure.resolution = action
    failure.retained_amount = retained
    failure.resolved_by_user_id = admin_user_id
    failure.resolved_at = _now()
    failure.resolution_note = note[:256] if note else None
    delivery = await session.get(Delivery, failure.delivery_id)
    lifecycle.add_event(
        session,
        delivery,
        order_id=order_id,
        event="failure_resolved",
        actor_user_id=admin_user_id,
        note=f"{action}" + (f": {note}" if note else ""),
    )
    await session.commit()
    await session.refresh(failure)
    return failure
