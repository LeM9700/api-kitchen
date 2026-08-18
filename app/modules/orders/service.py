import logging
import csv
from datetime import datetime, timedelta, timezone
from enum import Enum
from io import StringIO

from arq import ArqRedis
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.admin.tenants.models import TenantConfig
from app.modules.catalog.models import Category, Extra, Product, ProductExtra, ProductVariant
from app.modules.delivery.models import DeliveryZone
from app.modules.loyalty.account.models import LoyaltyTransaction
from app.modules.loyalty.account.service import get_or_create_account
from app.modules.loyalty.config.service import credit_points_for_order, get_or_create_loyalty_config
from app.modules.notifications.notification_service import notify_staff, notify_user
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.payments.models import Payment
from app.modules.promotions import service as promotions_service
from app.modules.promotions.models import Promotion
from app.modules.promotions.schemas import PromotionCartItem
from app.modules.stock.service import deduct_for_order, restore_for_order

logger = logging.getLogger(__name__)


class TransitionAuthority(str, Enum):
    """Authority that requested an order status transition.

    Attributes:
        INTERNAL: The transition was requested through this system's own
            staff-facing tooling (e.g. the admin/kitchen PATCH endpoint).
            Internal transitions are validated strictly against
            VALID_TRANSITIONS: anything outside the graph is rejected with a
            422 INVALID_STATUS_TRANSITION error, exactly as before this
            authority concept existed. This is the default, so every
            existing caller keeps its current behavior unchanged.
        EXTERNAL: The transition was imposed by a third-party system (e.g. a
            POS) that owns its own state machine, which may not match
            VALID_TRANSITIONS. External transitions are always applied and
            persisted; when one falls outside VALID_TRANSITIONS it is never
            rejected, but it is logged as a warning and counted via
            ``external_out_of_graph_transitions_total`` so the discrepancy
            stays observable instead of silently accepted.
    """

    INTERNAL = "internal"
    EXTERNAL = "external"


class _MetricCounter:
    """Minimal in-process counter.

    This project has no metrics backend wired in yet (no prometheus_client
    dependency, no equivalent). This is the simplest primitive that lets
    callers observe/increment a dedicated counter and lets tests assert on
    it, without introducing a new third-party dependency for a single
    counter. Swap for a real metrics client if one is added to the project.
    """

    def __init__(self) -> None:
        self.value = 0

    def increment(self) -> None:
        self.value += 1


external_out_of_graph_transitions_total = _MetricCounter()

VALID_TRANSITIONS = {
    "pending": {"confirmed", "cancelled", "rejected"},
    "confirmed": {"preparing", "cancelled"},
    "queued": {"confirmed", "cancelled"},
    "preparing": {"ready", "cancelled"},
    "ready": {"out_for_delivery", "delivered"},
    "out_for_delivery": {"delivered", "cancelled", "delivery_failed"},
    "delivered": set(),
    "cancelled": set(),
    "rejected": set(),
    "delivery_failed": set(),
}
NON_DELIVERY_ORDER_TYPES = {"pickup", "dine_in"}
ACTIVE_PREPARATION_ORDER_STATUSES = {"pending", "confirmed", "queued", "preparing", "ready", "out_for_delivery"}
PREPARATION_STATUSES = {"pending", "preparing", "ready"}
MANUAL_PAYMENT_PROVIDERS = {"cash", "external_terminal", "cash_register"}

# LOT 12: transitions autorisees pour une mutation de preparation par station.
# ready -> preparing existe uniquement ici (correction operationnelle KDS) et ne
# doit jamais etre ajoute a VALID_TRANSITIONS (statut global) ni accepte par
# l'endpoint generique PATCH /orders/{id}/status.
VALID_PREPARATION_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"preparing", "ready"},
    "preparing": {"ready"},
    "ready": {"preparing"},
}
# Statuts de commande pour lesquels une mutation de station a du sens. "queued"
# est volontairement exclu : une commande en file d'attente n'est pas encore en
# preparation.
STATION_PREPARABLE_ORDER_STATUSES = {"confirmed", "preparing", "ready"}


def _money(value) -> float:
    return round(float(value or 0), 2)


async def _resolve_loyalty_discount(
    session: AsyncSession,
    user_id: int | None,
    points_to_use: int,
    amount_eligible: float,
) -> tuple[float, int]:
    if points_to_use <= 0:
        return 0.0, 0
    if user_id is None:
        raise AppError("LOYALTY_USER_REQUIRED", "loyalty_user_id is required", 422, "loyalty_user_id")

    account = await get_or_create_account(session, user_id, commit=False)
    if account.points < points_to_use:
        raise AppError("INSUFFICIENT_POINTS", "Solde de points insuffisant", 422, "loyalty_points_to_use")

    config = await get_or_create_loyalty_config(session)
    rate = float(config.points_to_euro_rate)
    discount = _money(points_to_use * rate)
    eligible = _money(amount_eligible)
    if discount > eligible:
        max_points = int(eligible / rate) if rate > 0 else 0
        raise AppError(
            "LOYALTY_POINTS_EXCEED_TOTAL",
            f"Too many points for this order. Maximum usable points: {max_points}",
            422,
            "loyalty_points_to_use",
        )
    return discount, points_to_use


def _extras_from_snapshot(snapshot) -> list[dict]:
    return list(snapshot or [])


def _serialize_order_list(order: Order) -> dict:
    return {
        "id": order.id,
        "customer_email": order.customer_email,
        "customer_name": getattr(order, "customer_name", None),
        "customer_phone": getattr(order, "customer_phone", None),
        "order_type": getattr(order, "order_type", None) or "delivery",
        "status": order.status,
        "payment_status": getattr(order, "payment_status", "pending") or "pending",
        "source": getattr(order, "source", None) or "customer",
        "created_by_user_id": getattr(order, "created_by_user_id", None),
        "subtotal": _money(order.subtotal),
        "discount_total": _money(order.discount_total),
        "delivery_fee": _money(order.delivery_fee),
        "total": _money(order.total),
        "delivery_address": order.delivery_address,
        "delivery_zone_id": getattr(order, "delivery_zone_id", None),
        "table_number": getattr(order, "table_number", None),
        "estimated_delivery_at": getattr(order, "estimated_delivery_at", None),
        "created_at": getattr(order, "created_at", None),
    }


def _station_summary(items: list[dict]) -> list[dict]:
    grouped: dict[str, dict[str, int]] = {}
    for item in items:
        station = item.get("preparation_station") or "none"
        if station == "none":
            continue
        bucket = grouped.setdefault(station, {"total_items": 0, "ready_items": 0})
        bucket["total_items"] += 1
        if item.get("preparation_status") == "ready":
            bucket["ready_items"] += 1
    return [
        {
            "station": station,
            "total_items": counts["total_items"],
            "ready_items": counts["ready_items"],
            "all_ready": counts["total_items"] > 0 and counts["total_items"] == counts["ready_items"],
        }
        for station, counts in sorted(grouped.items())
    ]


async def _serialize_order_detail(session: AsyncSession, order: Order) -> dict:
    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
    )
    history_result = await session.execute(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order.id)
        .order_by(OrderStatusHistory.created_at, OrderStatusHistory.id)
    )

    serialized_items = [
        {
            "id": item.id,
            "product_id": item.product_id,
            "variant_id": item.variant_id,
            "product_name": getattr(item, "product_name_snapshot", None),
            "variant_name": getattr(item, "variant_name_snapshot", None),
            "quantity": item.quantity,
            "unit_price": _money(item.unit_price),
            "extras_total": _money(getattr(item, "extras_total", 0)),
            "total": _money(item.total),
            "extras": _extras_from_snapshot(getattr(item, "extras_snapshot", None)),
            "preparation_status": getattr(item, "preparation_status", None) or "pending",
            "preparation_station": getattr(item, "preparation_station", None) or "kitchen",
            "prepared_at": getattr(item, "prepared_at", None),
            "prepared_by_user_id": getattr(item, "prepared_by_user_id", None),
        }
        for item in items_result.scalars()
    ]

    payload = _serialize_order_list(order)
    payload.update(
        {
            "user_id": order.user_id,
            "promo_code": getattr(order, "promo_code", None),
            "items": serialized_items,
            "station_summary": _station_summary(serialized_items),
            "status_history": [
                {
                    "status": history.status,
                    "note": history.note,
                    "authority": getattr(history, "authority", None) or "internal",
                    "created_at": history.created_at,
                }
                for history in history_result.scalars()
            ],
        }
    )
    return payload


async def _estimate_delivery_at(
    session: AsyncSession,
    delivery_minutes: int = 0,
) -> datetime:
    config = await session.scalar(select(TenantConfig))
    prep_minutes = 25
    if isinstance(config, TenantConfig):
        active_count = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.status.in_(("confirmed", "preparing", "queued"))
            )
        ) or 0
        if config.auto_calc_prep_time and active_count >= config.peak_orders_threshold:
            overload = max(0, active_count - config.peak_orders_threshold + 1)
            prep_minutes = config.prep_time_peak_minutes + overload * config.overhead_per_order_minutes
        else:
            prep_minutes = config.prep_time_normal_minutes
    return datetime.now(timezone.utc) + timedelta(minutes=prep_minutes + delivery_minutes)


async def _resolve_delivery(
    session: AsyncSession,
    order_type: str,
    delivery_zone_id: int | None,
    subtotal: float,
) -> tuple[float, int, int | None]:
    """Calcule delivery_fee, delai de trajet additionnel, et delivery_zone_id effectif.

    Pour order_type == "pickup" ou "dine_in" : pas de frais, pas de delai de
    trajet. delivery_zone_id est ignore meme s'il est envoye par le client.
    """
    if order_type in NON_DELIVERY_ORDER_TYPES:
        return 0.0, 0, None

    if delivery_zone_id is None:
        return 0.0, 0, None

    zone = await session.get(DeliveryZone, delivery_zone_id)
    if zone is None or not zone.is_active:
        raise AppError("INVALID_DELIVERY_ZONE", "Delivery zone not found or inactive", 422, "delivery_zone_id")
    if subtotal < float(zone.min_order_amount or 0):
        raise AppError(
            "DELIVERY_MIN_ORDER_NOT_MET",
            "Order subtotal is below the delivery zone minimum",
            422,
            "delivery_zone_id",
        )
    return _money(zone.fee), int(zone.estimated_minutes or 0), delivery_zone_id


async def _resolve_extras(session: AsyncSession, product_id: int, item_extras: list) -> tuple[list[dict], float]:
    snapshot: list[dict] = []
    extras_unit_total = 0.0

    for requested in item_extras:
        extra = await session.scalar(
            select(Extra)
            .join(ProductExtra, ProductExtra.extra_id == Extra.id)
            .where(
                and_(
                    ProductExtra.product_id == product_id,
                    Extra.id == requested.extra_id,
                    Extra.is_active.is_(True),
                )
            )
        )
        if extra is None:
            raise AppError("EXTRA_NOT_FOUND", "Extra not found or unavailable for product", 404, "extras")

        quantity = int(requested.quantity)
        unit_price = _money(extra.price)
        total = _money(unit_price * quantity)
        extras_unit_total += total
        snapshot.append(
            {
                "extra_id": extra.id,
                "name": extra.name,
                "quantity": quantity,
                "unit_price": unit_price,
                "total": total,
            }
        )

    return snapshot, _money(extras_unit_total)


async def _resolve_preparation_station(session: AsyncSession, product: Product) -> str:
    station = getattr(product, "preparation_station", None)
    if station:
        return station
    if product.category_id is None:
        return "kitchen"
    category = await session.get(Category, product.category_id)
    return getattr(category, "preparation_station", None) or "kitchen"


async def _find_idempotent_order(
    session: AsyncSession,
    user_id: int | None,
    idempotency_key: str,
) -> Order | None:
    window_start = datetime.now(timezone.utc) - timedelta(hours=24)
    return await session.scalar(
        select(Order).where(
            Order.user_id == user_id,
            Order.idempotency_key == idempotency_key,
            Order.created_at >= window_start,
        )
    )


def _payment_payload(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "provider": payment.provider,
        "provider_payment_id": payment.provider_payment_id,
        "external_reference": getattr(payment, "external_reference", None),
        "amount": _money(payment.amount),
        "amount_received": (
            _money(payment.amount_received)
            if getattr(payment, "amount_received", None) is not None
            else None
        ),
        "currency": payment.currency,
        "status": payment.status,
        "created_by_user_id": getattr(payment, "created_by_user_id", None),
        "receipt_url": None,
    }


async def create_order(
    session: AsyncSession,
    body,
    user_id: int | None = None,
    tenant_slug: str = "default",
    idempotency_key: str | None = None,
    created_by_user_id: int | None = None,
    source: str = "customer",
    table_number: str | None = None,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    commit: bool = True,
    skip_idempotency_lookup: bool = False,
) -> Order:
    """Cree une commande avec pricing et discount calcules exclusivement cote serveur.

    [SECURITE] body.discount_total et body.items[].unit_price sont
    intentionnellement ignores. Les prix sont lus depuis le catalogue (Product,
    ProductVariant) et la remise depuis body.promo_code via le service
    promotions -- un client ne peut pas forger un prix ou s'accorder une remise.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        body: Payload OrderCreate valide par Pydantic (sans unit_price).
        user_id: Identifiant de l'utilisateur authentifie, ou None pour commande anonyme.
        tenant_slug: Slug tenant pour la validation du code promo.

    Returns:
        Instance Order persistee et rafraichie.

    Raises:
        AppError: PRODUCT_NOT_FOUND (404) si un produit ou variant est inconnu/inactif.
        AppError: INVALID_PROMO (422) si le code promo est invalide ou expire.
    """
    if not idempotency_key:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    if len(idempotency_key) > 128:
        raise AppError("IDEMPOTENCY_KEY_TOO_LONG", "Idempotency-Key must be 128 characters or fewer", 422)

    if not skip_idempotency_lookup:
        existing_order = await _find_idempotent_order(session, user_id, idempotency_key)
        if existing_order is not None:
            return existing_order

    # [SECURITE] Resolution des prix depuis le catalogue -- unit_price client ignore.
    resolved_items: list[tuple] = []
    for item in body.items:
        product = await session.get(Product, item.product_id)
        if product is None or not product.is_active:
            raise AppError(
                "PRODUCT_NOT_FOUND",
                f"Product {item.product_id} not found or inactive",
                404,
                "product_id",
            )
        unit_price = float(product.base_price)
        if item.variant_id is not None:
            variant = await session.get(ProductVariant, item.variant_id)
            if (
                variant is None
                or not variant.is_active
                or variant.product_id != item.product_id
            ):
                raise AppError(
                    "PRODUCT_NOT_FOUND",
                    f"Variant {item.variant_id} not found or inactive",
                    404,
                    "variant_id",
                )
            unit_price += float(variant.price_delta)
            variant_name = variant.name
        else:
            variant_name = None

        extras_snapshot, extras_unit_total = await _resolve_extras(
            session, item.product_id, getattr(item, "extras", [])
        )
        unit_price += extras_unit_total
        preparation_station = await _resolve_preparation_station(session, product)
        resolved_items.append(
            (
                item,
                unit_price,
                product.name,
                variant_name,
                extras_snapshot,
                extras_unit_total,
                product.category_id,
                preparation_station,
            )
        )

    subtotal = _money(sum(item.quantity * price for item, price, *_ in resolved_items))

    # [SECURITE] discount_total calcule cote serveur uniquement.
    discount_total: float = 0.0
    if body.promo_code:
        promo_items = [
            PromotionCartItem(
                product_id=item.product_id,
                category_id=category_id,
                quantity=item.quantity,
                unit_price=unit_price,
                line_total=_money(item.quantity * unit_price),
            )
            for (
                item,
                unit_price,
                _product_name,
                _variant_name,
                _extras_snapshot,
                _extras_unit_total,
                category_id,
                _preparation_station,
            )
            in resolved_items
        ]
        discount_total = await promotions_service.apply_promo(
            session,
            tenant_slug,
            body.promo_code,
            subtotal,
            user_id=user_id,
            items=promo_items,
        )

    loyalty_points_to_use = int(getattr(body, "loyalty_points_to_use", 0) or 0)
    loyalty_discount = 0.0
    if loyalty_points_to_use:
        loyalty_discount, loyalty_points_to_use = await _resolve_loyalty_discount(
            session,
            user_id,
            loyalty_points_to_use,
            max(0.0, subtotal - discount_total),
        )
        discount_total = _money(discount_total + loyalty_discount)

    order_type = getattr(body, "order_type", None) or "delivery"
    delivery_fee, delivery_minutes, effective_delivery_zone_id = await _resolve_delivery(
        session, order_type, getattr(body, "delivery_zone_id", None), subtotal
    )
    estimated_delivery_at = await _estimate_delivery_at(session, delivery_minutes)

    total = _money(subtotal - discount_total + delivery_fee)
    order = Order(
        user_id=user_id,
        customer_email=body.customer_email,
        customer_name=customer_name,
        customer_phone=customer_phone,
        order_type=order_type,
        source=source,
        created_by_user_id=created_by_user_id,
        delivery_address=body.delivery_address if order_type == "delivery" else None,
        subtotal=subtotal,
        discount_total=discount_total,
        delivery_fee=delivery_fee,
        delivery_zone_id=effective_delivery_zone_id,
        table_number=table_number,
        estimated_delivery_at=estimated_delivery_at,
        total=total,
        promo_code=body.promo_code,
        idempotency_key=idempotency_key,
        payment_status="pending",
    )
    session.add(order)
    await session.flush()
    if loyalty_points_to_use:
        if user_id is None:
            raise AppError("LOYALTY_USER_REQUIRED", "loyalty_user_id is required", 422, "loyalty_user_id")
        account = await get_or_create_account(session, user_id, commit=False)
        account.points -= loyalty_points_to_use
        session.add(
            LoyaltyTransaction(
                account_id=account.id,
                points_delta=-loyalty_points_to_use,
                reason=f"manual_order_{order.id}",
                transaction_type="redeem",
                source="staff_checkout",
                changed_by_user_id=created_by_user_id,
                order_id=order.id,
                metadata_json={"discount_amount": str(loyalty_discount)},
            )
        )
    for (
        item,
        unit_price,
        product_name,
        variant_name,
        extras_snapshot,
        extras_unit_total,
        _category_id,
        preparation_station,
    ) in resolved_items:
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name_snapshot=product_name,
                variant_name_snapshot=variant_name,
                extras_snapshot=extras_snapshot,
                extras_total=_money(item.quantity * extras_unit_total),
                quantity=item.quantity,
                unit_price=unit_price,
                total=_money(item.quantity * unit_price),
                preparation_status="pending",
                preparation_station=preparation_station,
            )
        )
    session.add(OrderStatusHistory(order_id=order.id, status=order.status))
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(order)

    # Enregistre l'utilisation du code promo apres creation de la commande.
    if commit and body.promo_code and user_id is not None:
        try:
            await promotions_service.record_promo_usage(session, body.promo_code, user_id, order.id)
            await session.commit()
        except Exception as exc:
            logger.error(
                "promotions.record_promo_usage failed for order_id=%s user_id=%s: %s",
                order.id,
                user_id,
                exc,
            )

    return order


async def create_manual_order(
    session: AsyncSession,
    body,
    actor_user_id: int,
    tenant_slug: str,
    idempotency_key: str | None,
    arq_pool: ArqRedis | None = None,
) -> dict:
    if not idempotency_key:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    if len(idempotency_key) > 128:
        raise AppError("IDEMPOTENCY_KEY_TOO_LONG", "Idempotency-Key must be 128 characters or fewer", 422)

    existing_order = await _find_idempotent_order(session, None, idempotency_key)
    if existing_order is not None:
        payment = await session.scalar(
            select(Payment)
            .where(Payment.order_id == existing_order.id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
        )
        if payment is None:
            raise AppError("PAYMENT_NOT_FOUND", "Manual order payment not found", 409)
        return {
            "order": await _serialize_order_detail(session, existing_order),
            "payment": _payment_payload(payment),
            "receipt": await build_receipt(session, existing_order.id),
        }

    payment_body = body.payment
    if payment_body.method not in MANUAL_PAYMENT_PROVIDERS:
        raise AppError("INVALID_PAYMENT_METHOD", "Unsupported manual payment method", 422, "payment.method")

    customer = body.customer
    try:
        order = await create_order(
            session,
            body,
            user_id=getattr(body, "loyalty_user_id", None),
            tenant_slug=tenant_slug,
            idempotency_key=idempotency_key,
            created_by_user_id=actor_user_id,
            source="manual",
            table_number=body.table_number,
            customer_name=customer.full_name if customer else None,
            customer_phone=customer.phone if customer else None,
            commit=False,
            skip_idempotency_lookup=True,
        )
        payment = Payment(
            order_id=order.id,
            provider=payment_body.method,
            provider_payment_id=payment_body.external_reference,
            external_reference=payment_body.external_reference,
            amount=order.total,
            amount_received=payment_body.amount_received,
            currency="EUR",
            status="paid",
            created_by_user_id=actor_user_id,
        )
        session.add(payment)
        order.payment_status = "paid"
        await session.flush()
        if order.status == "pending":
            await update_status(
                session,
                order.id,
                "confirmed",
                body.note or f"Manual payment: {payment_body.method}",
                tenant_slug=tenant_slug,
                arq_pool=arq_pool,
                actor_user_id=actor_user_id,
                is_staff=True,
            )
        else:
            await session.commit()
    except Exception:
        await session.rollback()
        raise

    await session.refresh(order)
    await session.refresh(payment)
    return {
        "order": await _serialize_order_detail(session, order),
        "payment": _payment_payload(payment),
        "receipt": await build_receipt(session, order.id),
    }


def _apply_preparation_status(item: OrderItem, status: str, actor_user_id: int | None) -> None:
    """Applique un statut de preparation a un item et synchronise prepared_at/prepared_by_user_id.

    Partagee par update_item_preparation et update_station_preparation (LOT 12)
    pour eviter deux regles de timestamp divergentes -- cf. AGENTS.md LOT 12 A16.
    Tout statut different de "ready" efface les champs d'audit : c'est
    indispensable pour un vrai undo ready -> preparing (A8).
    """
    item.preparation_status = status
    if status == "ready":
        item.prepared_at = datetime.now(timezone.utc)
        item.prepared_by_user_id = actor_user_id
    else:
        item.prepared_at = None
        item.prepared_by_user_id = None


async def update_item_preparation(
    session: AsyncSession,
    order_id: int,
    item_id: int,
    status: str,
    note: str | None,
    actor_user_id: int,
) -> dict:
    if status not in PREPARATION_STATUSES:
        raise AppError("INVALID_PREPARATION_STATUS", "Invalid preparation status", 422, "status")
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.status not in ACTIVE_PREPARATION_ORDER_STATUSES:
        raise AppError("ORDER_NOT_ACTIVE", "Cannot update preparation for a terminal order", 409)

    item = await session.get(OrderItem, item_id)
    if item is None or item.order_id != order_id:
        raise AppError("ORDER_ITEM_NOT_FOUND", "Order item not found", 404)

    _apply_preparation_status(item, status, actor_user_id)

    if note:
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                status=f"item_{status}",
                note=f"Item #{item.id}: {note}",
            )
        )
    await session.commit()
    await session.refresh(item)
    return {
        "id": item.id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "product_name": getattr(item, "product_name_snapshot", None),
        "variant_name": getattr(item, "variant_name_snapshot", None),
        "quantity": item.quantity,
        "unit_price": _money(item.unit_price),
        "extras_total": _money(getattr(item, "extras_total", 0)),
        "total": _money(item.total),
        "extras": _extras_from_snapshot(getattr(item, "extras_snapshot", None)),
        "preparation_status": item.preparation_status,
        "preparation_station": item.preparation_station,
        "prepared_at": item.prepared_at,
        "prepared_by_user_id": item.prepared_by_user_id,
    }


async def update_station_preparation(
    session: AsyncSession,
    order_id: int,
    station: str,
    status: str,
    note: str | None = None,
    actor_user_id: int | None = None,
    tenant_slug: str | None = None,
) -> dict:
    """Applique un statut de preparation a tous les items d'une station, atomiquement.

    Reutilise _apply_preparation_status (partage avec update_item_preparation)
    pour la partie item, puis synchronise le statut GLOBAL de la commande :

    - preparing -> ready (station) + toutes les stations ready + order.status
      == "preparing" => order.status devient "ready".
    - ready -> preparing (station, correction operationnelle) + order.status
      == "ready" => order.status revient a "preparing". Ce retour arriere est
      gere ici, en interne, et ne doit JAMAIS etre ajoute a VALID_TRANSITIONS :
      l'endpoint generique PATCH /orders/{id}/status doit continuer a refuser
      ready -> preparing (cf. AGENTS.md LOT 12 A10).

    Verrouille la commande et les items de la station via SELECT ... FOR UPDATE
    pour rendre deterministe une double mutation quasi simultanee sur la meme
    station (ex. ecran mural + telephone remote) -- une seule transaction,
    un seul commit, jamais de commit par item (A5/A6).

    Idempotent (A13) : si tous les items de la station sont deja dans le statut
    demande, retourne l'etat actuel sans ecrire de nouvelle transition ni
    d'historique.

    Raises:
        AppError: INVALID_PREPARATION_STATUS (422) si status est invalide.
        AppError: ORDER_NOT_FOUND (404) si la commande n'existe pas.
        AppError: ORDER_NOT_PREPARABLE (409) si la commande n'est pas dans un
            statut ou la preparation a un sens (cf. STATION_PREPARABLE_ORDER_STATUSES).
        AppError: ORDER_STATION_NOT_FOUND (404) si aucun item de la commande
            n'appartient a cette station.
        AppError: INVALID_PREPARATION_TRANSITION (422) si un item de la station
            ne peut pas atteindre `status` depuis son statut courant.
    """
    if status not in PREPARATION_STATUSES:
        raise AppError("INVALID_PREPARATION_STATUS", "Invalid preparation status", 422, "status")

    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.status not in STATION_PREPARABLE_ORDER_STATUSES:
        raise AppError(
            "ORDER_NOT_PREPARABLE",
            "Cannot update station preparation for this order status",
            409,
            "status",
        )

    items_result = await session.execute(
        select(OrderItem)
        .where(OrderItem.order_id == order_id, OrderItem.preparation_station == station)
        .with_for_update()
    )
    items = list(items_result.scalars().all())
    if not items:
        raise AppError("ORDER_STATION_NOT_FOUND", "No items found for this station", 404, "station")

    current_statuses = {item.preparation_status for item in items}
    if current_statuses == {status}:
        # Deja dans l'etat demande pour toute la station : idempotent, pas de mutation.
        return await _serialize_order_detail(session, order)

    for item in items:
        if item.preparation_status == status:
            continue
        allowed = VALID_PREPARATION_TRANSITIONS.get(item.preparation_status, set())
        if status not in allowed:
            raise AppError(
                "INVALID_PREPARATION_TRANSITION",
                f"Cannot move item from {item.preparation_status} to {status}",
                422,
                "status",
            )

    for item in items:
        _apply_preparation_status(item, status, actor_user_id)

    previous_order_status = order.status
    global_changed = False

    if status == "ready" and previous_order_status == "preparing":
        remaining_result = await session.execute(
            select(OrderItem.preparation_status).where(OrderItem.order_id == order_id)
        )
        remaining_statuses = [row[0] for row in remaining_result.all()]
        if remaining_statuses and all(item_status == "ready" for item_status in remaining_statuses):
            order.status = "ready"
            global_changed = True
            global_note = note
    elif status == "preparing" and previous_order_status == "ready":
        order.status = "preparing"
        global_changed = True
        global_note = note or "KDS station reopened"

    if global_changed:
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                status=order.status,
                note=global_note,
                authority=TransitionAuthority.INTERNAL.value,
            )
        )

    await session.commit()
    await session.refresh(order)

    _effective_tenant = tenant_slug or "default"
    try:
        await notify_staff(
            session=session,
            tenant_slug=_effective_tenant,
            event="order.preparation_updated",
            title="Preparation mise a jour",
            body=f"Commande #{order_id} : poste {station} -> {status}",
            data={"order_id": order_id},
        )
    except Exception as exc:
        logger.error(
            "notify_staff failed for order_id=%s station=%s status=%s: %s",
            order_id,
            station,
            status,
            exc,
        )

    if global_changed and order.status == "ready" and order.user_id is not None:
        try:
            await notify_user(
                session=session,
                tenant_slug=_effective_tenant,
                user_id=order.user_id,
                event="order.ready",
                title="Prete a recuperer",
                body=f"Votre commande #{order_id} est prete !",
                data={"order_id": order_id},
            )
        except Exception as exc:
            logger.error("notify_user failed for order_id=%s: %s", order_id, exc)

    return await _serialize_order_detail(session, order)


async def list_orders(
    session: AsyncSession,
    pagination: PaginationParams,
    statuses: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[dict], int]:
    """Retourne une page de commandes triees par date decroissante.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        pagination: Parametres de pagination (page, page_size).

    Returns:
        Tuple (liste des commandes de la page, total toutes pages confondues).
    """
    filters = []
    if statuses:
        filters.append(Order.status.in_(statuses))
    if date_from is not None:
        filters.append(Order.created_at >= date_from)
    if date_to is not None:
        filters.append(Order.created_at <= date_to)

    stmt = select(Order)
    count_stmt = select(func.count()).select_from(Order)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    total = await session.scalar(count_stmt) or 0
    result = await session.execute(
        stmt
        .order_by(Order.created_at.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return [_serialize_order_list(order) for order in result.scalars()], total


async def export_orders_csv(
    session: AsyncSession,
    status: str | None = None,
    payment_status: str | None = None,
    order_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> str:
    filters = []
    if status:
        filters.append(Order.status.in_([part.strip() for part in status.split(",") if part.strip()]))
    if payment_status:
        filters.append(
            Order.payment_status.in_([part.strip() for part in payment_status.split(",") if part.strip()])
        )
    if order_type:
        filters.append(Order.order_type == order_type)
    if date_from is not None:
        filters.append(Order.created_at >= date_from)
    if date_to is not None:
        filters.append(Order.created_at <= date_to)

    stmt = select(Order)
    if filters:
        stmt = stmt.where(*filters)
    result = await session.execute(stmt.order_by(Order.created_at.desc(), Order.id.desc()))

    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "created_at",
            "customer_email",
            "customer_name",
            "customer_phone",
            "order_type",
            "status",
            "payment_status",
            "source",
            "table_number",
            "subtotal",
            "discount_total",
            "delivery_fee",
            "total",
        ],
    )
    writer.writeheader()
    for order in result.scalars():
        row = _serialize_order_list(order)
        writer.writerow({
            field: row.get(field)
            for field in writer.fieldnames
        })
    return output.getvalue()


async def list_my_orders(
    session: AsyncSession,
    pagination: PaginationParams,
    user_id: int,
    statuses: list[str] | None = None,
) -> tuple[list[dict], int]:
    filters = [Order.user_id == user_id]
    if statuses:
        filters.append(Order.status.in_(statuses))

    stmt = select(Order).where(*filters)
    total = await session.scalar(select(func.count()).select_from(Order).where(*filters)) or 0
    result = await session.execute(
        stmt.order_by(Order.created_at.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return [_serialize_order_list(order) for order in result.scalars()], total


async def get_order_detail(
    session: AsyncSession,
    order_id: int,
    user_id: int | None = None,
    is_staff: bool = False,
) -> dict:
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if not is_staff and order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    return await _serialize_order_detail(session, order)


async def cancel_my_order(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    tenant_slug: str,
    arq_pool: ArqRedis | None = None,
) -> Order:
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.status != "pending":
        raise AppError("ORDER_CANCEL_NOT_ALLOWED", "Only pending orders can be cancelled by customer", 422)
    return await update_status(
        session,
        order_id,
        "cancelled",
        "Cancelled by customer",
        tenant_slug,
        arq_pool,
        actor_user_id=user_id,
        is_staff=False,
    )


async def build_reorder_payload(
    session: AsyncSession,
    order_id: int,
    user_id: int | None = None,
    is_staff: bool = False,
) -> dict:
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if not is_staff and order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
    )
    items: list[dict] = []
    unavailable: list[dict] = []
    for item in items_result.scalars():
        warning = None
        available = True
        product = await session.get(Product, item.product_id)
        if product is None or not product.is_active:
            available = False
            warning = "Product unavailable"
        elif item.variant_id is not None:
            variant = await session.get(ProductVariant, item.variant_id)
            if variant is None or not variant.is_active or variant.product_id != item.product_id:
                available = False
                warning = "Variant unavailable"

        extras = [
            {"extra_id": extra["extra_id"], "quantity": extra["quantity"]}
            for extra in _extras_from_snapshot(getattr(item, "extras_snapshot", None))
        ]
        payload_item = {
            "product_id": item.product_id,
            "variant_id": item.variant_id,
            "quantity": item.quantity,
            "extras": extras,
            "available": available,
            "warning": warning,
        }
        items.append(payload_item)
        if not available:
            unavailable.append(payload_item)

    return {"source_order_id": order_id, "items": items, "unavailable_items": unavailable}


async def build_receipt(
    session: AsyncSession,
    order_id: int,
) -> dict:
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    detail = await _serialize_order_detail(session, order)
    receipt_items = []
    for item in detail["items"]:
        label = item["product_name"] or f"Product #{item['product_id']}"
        if item["variant_name"]:
            label = f"{label} - {item['variant_name']}"
        receipt_items.append(
            {
                "label": label,
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "extras": item["extras"],
                "total": item["total"],
            }
        )

    return {
        "order_id": order.id,
        "status": order.status,
        "payment_status": getattr(order, "payment_status", "pending") or "pending",
        "customer_email": order.customer_email,
        "customer_name": getattr(order, "customer_name", None),
        "delivery_address": order.delivery_address,
        "table_number": getattr(order, "table_number", None),
        "created_at": order.created_at,
        "estimated_delivery_at": getattr(order, "estimated_delivery_at", None),
        "items": receipt_items,
        "totals": {
            "subtotal": _money(order.subtotal),
            "discount_total": _money(order.discount_total),
            "delivery_fee": _money(order.delivery_fee),
            "total": _money(order.total),
        },
        "meta": {
            "delivery_zone_id": getattr(order, "delivery_zone_id", None),
            "order_type": getattr(order, "order_type", None) or "delivery",
            "source": getattr(order, "source", None) or "customer",
        },
    }


async def update_status(
    session: AsyncSession,
    order_id: int,
    status: str,
    note: str | None = None,
    tenant_slug: str | None = None,
    arq_pool: ArqRedis | None = None,
    actor_user_id: int | None = None,
    is_staff: bool = True,
    authority: TransitionAuthority = TransitionAuthority.INTERNAL,
) -> Order:
    """Met a jour le statut d'une commande avec notifications temps reel.

    - Transition vers "confirmed" : deduit le stock atomiquement dans la meme transaction.
    - Transition vers "cancelled" depuis "confirmed" : restitue le stock deduit
      (restore_for_order) dans la meme transaction avant commit.
    - Transition vers "cancelled" depuis "pending" : pas de restitution (stock non deduit).
    - Post-commit : envoie les notifications WebSocket et push selon la transition.

    [PROD] Ne jamais appeler session.commit() avant cette fonction dans le
    meme contexte de session : cela briserait l'atomicite stock/statut.

    Args:
        session: Session SQLAlchemy async. Le caller NE DOIT PAS commit avant le
            retour de cette fonction.
        order_id: Cle primaire de la commande a mettre a jour.
        status: Statut cible. Avec authority=INTERNAL, doit etre une transition
            valide depuis le statut actuel. Avec authority=EXTERNAL, toute
            valeur est appliquee (voir TransitionAuthority).
        note: Note humaine optionnelle ajoutee a l'entree d'historique de statut.
        tenant_slug: Identifiant tenant requis quand status == "confirmed" ou "cancelled".
        arq_pool: Pool arq singleton injecte depuis le lifespan.
        authority: Qui impose la transition (INTERNAL par defaut). Voir
            TransitionAuthority pour la semantique complete.

    Returns:
        Instance Order rafraichie apres commit.

    Raises:
        AppError: ORDER_NOT_FOUND (404) si la commande n'existe pas.
        AppError: INVALID_STATUS_TRANSITION (422) si authority=INTERNAL et la
            transition n'est pas autorisee par VALID_TRANSITIONS.
        AppError: INSUFFICIENT_STOCK (409) si confirmation et stock insuffisant.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    previous_status = order.status

    if status == "confirmed" and (getattr(order, "payment_status", None) or "pending") != "paid":
        raise AppError("PAYMENT_REQUIRED", "Order must be paid before confirmation", 409, "payment_status")

    # Validate the transition using the ORIGINAL requested status before any redirect.
    is_graph_transition = status in VALID_TRANSITIONS.get(previous_status, set())
    if authority == TransitionAuthority.INTERNAL:
        if not is_graph_transition:
            raise AppError("INVALID_STATUS_TRANSITION", "Invalid order status transition", 422, "status")
    elif not is_graph_transition:
        # [EXTERNAL] Une autorite externe (ex. POS) peut imposer une transition
        # hors VALID_TRANSITIONS -- jamais rejetee, mais toujours observable.
        logger.warning(
            "ORDER_STATUS_TRANSITION_OUT_OF_GRAPH: order_id=%s previous_status=%s "
            "requested_status=%s authority=%s",
            order_id,
            previous_status,
            status,
            authority.value,
        )
        external_out_of_graph_transitions_total.increment()

    # [FILE D'ATTENTE] Si la confirmation est demandée et que la capacité est dépassée,
    # router vers "queued" au lieu de "confirmed" (APRES validation).
    actual_status = status
    if status == "confirmed":
        from sqlalchemy import select as _select, func as _func
        config = await session.scalar(_select(TenantConfig))
        if isinstance(config, TenantConfig):
            active_count = await session.scalar(
                _select(_func.count()).select_from(Order).where(
                    Order.status.in_(("confirmed", "preparing", "queued"))
                )
            ) or 0
            if active_count >= config.peak_orders_threshold:
                actual_status = "queued"

    order.status = actual_status
    session.add(
        OrderStatusHistory(order_id=order.id, status=actual_status, note=note, authority=authority.value)
    )

    low_stock: list = []

    if actual_status == "confirmed":
        # Deduction de stock atomique avec la confirmation.
        low_stock = await deduct_for_order(
            session, order_id, tenant_slug or "default", auto_commit=False, actor_user_id=actor_user_id
        )
        # Desactive le code promo atomiquement avec la confirmation.
        if order.promo_code:
            promo = await session.scalar(
                select(Promotion).where(Promotion.code == order.promo_code.upper())
            )
            if promo:
                promo.is_active = False

    elif actual_status == "cancelled" and previous_status == "confirmed":
        # [FIX 3] Le stock a deja ete deduit a la confirmation -- on le restitue
        # dans la meme transaction avant commit.
        await restore_for_order(session, tenant_slug or "default", order_id, actor_user_id=actor_user_id)

    await session.commit()
    await session.refresh(order)

    # Enqueue alerte stock post-commit (non bloquant, erreur ignoree).
    if arq_pool is not None and low_stock:
        try:
            for ingredient in low_stock:
                await arq_pool.enqueue_job(
                    "send_stock_alert",
                    ingredient_id=ingredient.id,
                    ingredient_name=ingredient.name,
                    current_qty=float(ingredient.current_qty),
                    tenant_slug=tenant_slug or "default",
                )
        except Exception:
            pass

    # Enqueue notification d'annulation post-commit.
    if actual_status == "cancelled" and arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_email",
                to=order.customer_email or "",
                subject="Votre commande a ete annulee",
                body=f"Votre commande #{order_id} a ete annulee.",
            )
        except Exception:
            pass

    # Credite les points de fidelite apres livraison confirmee.
    # Recupere les category_ids des produits commandes pour le calcul des regles bonus.
    if actual_status == "delivered" and order.user_id is not None:
        try:
            items_result = await session.execute(
                select(OrderItem).where(OrderItem.order_id == order_id)
            )
            order_items = list(items_result.scalars())
            product_ids = [item.product_id for item in order_items]

            category_ids: list[int] = []
            if product_ids:
                products_result = await session.execute(
                    select(Product.category_id).where(
                        Product.id.in_(product_ids),
                        Product.category_id.isnot(None),
                    )
                )
                category_ids = [row[0] for row in products_result]

            await credit_points_for_order(
                session,
                order.user_id,
                order_id,
                float(order.total),
                category_ids,
            )
        except Exception as exc:
            logger.error(
                "loyalty.credit_points_for_order failed for order_id=%s user_id=%s: %s",
                order_id,
                order.user_id,
                exc,
            )

    # Notifications temps reel post-commit (toujours dans try/except -- ne doit jamais
    # faire planter le flux metier).
    _effective_tenant = tenant_slug or "default"
    try:
        # Table de routing : (previous_status, new_status) -> messages client + staff.
        # staff_title = None signifie pas de notification staff pour cette transition.
        _notif_map: dict[tuple[str, str], dict] = {
            ("pending", "confirmed"): {
                "client_title": "Commande confirmee",
                "client_body": f"Votre commande #{order_id} a ete confirmee.",
                "staff_title": "Nouvelle commande",
                "staff_body": f"Nouvelle commande #{order_id} recue.",
            },
            ("confirmed", "preparing"): {
                "client_title": "En preparation",
                "client_body": f"Votre commande #{order_id} est en cours de preparation.",
                "staff_title": None,
                "staff_body": None,
            },
            ("preparing", "ready"): {
                "client_title": "Prete a recuperer",
                "client_body": f"Votre commande #{order_id} est prete !",
                "staff_title": None,
                "staff_body": None,
            },
            ("ready", "delivered"): {
                "client_title": "Livree ! Bon appetit",
                "client_body": f"Votre commande #{order_id} a ete livree. Bonne degustation !",
                "staff_title": None,
                "staff_body": None,
            },
            ("out_for_delivery", "delivered"): {
                "client_title": "Livree ! Bon appetit",
                "client_body": f"Votre commande #{order_id} a ete livree. Bonne degustation !",
                "staff_title": None,
                "staff_body": None,
            },
        }

        # Transitions vers "cancelled" depuis n'importe quel etat.
        if actual_status == "cancelled":
            notif: dict | None = {
                "client_title": "Commande annulee",
                "client_body": f"Votre commande #{order_id} a ete annulee.",
                "staff_title": "Commande annulee",
                "staff_body": f"Commande #{order_id} annulee (etait : {previous_status}).",
            }
        else:
            notif = _notif_map.get((previous_status, actual_status))

        if notif:
            order_data = {"order_id": order_id}

            # Notification client (uniquement si commande liee a un user authentifie).
            if order.user_id is not None:
                await notify_user(
                    session=session,
                    tenant_slug=_effective_tenant,
                    user_id=order.user_id,
                    event=f"order.{actual_status}",
                    title=notif["client_title"],
                    body=notif["client_body"],
                    data=order_data,
                )

            # Notification staff (uniquement si definie pour cette transition).
            if notif.get("staff_title"):
                await notify_staff(
                    session=session,
                    tenant_slug=_effective_tenant,
                    event=f"order.{actual_status}",
                    title=notif["staff_title"],
                    body=notif["staff_body"],
                    data=order_data,
                )

    except Exception as exc:
        logger.error(
            "notifications failed for order_id=%s status=%s: %s",
            order_id,
            status,
            exc,
        )

    return order
