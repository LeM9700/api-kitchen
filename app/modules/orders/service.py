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
from app.core.i18n.translate import t
from app.modules.admin.tenants.models import TenantConfig
from app.modules.catalog.models import Category, Extra, Product, ProductExtra, ProductVariant
from app.modules.delivery import lifecycle as delivery_lifecycle
from app.modules.delivery import proof as delivery_proof_service
from app.modules.delivery import service as delivery_service
from app.modules.delivery.models import DeliveryZone
from app.modules.hr.models import Establishment
from app.modules.loyalty.account.models import LoyaltyTransaction
from app.modules.loyalty.account.service import get_or_create_account
from app.modules.loyalty.config.models import LoyaltyReward
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


def _loyalty_order_audit_metadata(
    *,
    body,
    order: Order,
    customer_id: int | None,
    staff_user_id: int | None,
) -> dict:
    return {
        "order_id": order.id,
        "establishment_id": order.establishment_id,
        "customer_id": customer_id,
        "staff_user_id": staff_user_id,
        "loyalty_identification_method": getattr(body, "loyalty_identification_method", None),
        "loyalty_oral_confirmed": bool(getattr(body, "loyalty_oral_confirmed", False)),
    }


async def _record_loyalty_reward_audit(
    session: AsyncSession,
    *,
    body,
    order: Order,
    customer_id: int,
    staff_user_id: int | None,
    reward: LoyaltyReward,
    discount_amount: float,
) -> None:
    from app.modules.admin.customers.service import record_admin_audit

    await record_admin_audit(
        session,
        actor={"id": staff_user_id} if staff_user_id is not None else None,
        action="loyalty_staff_reward_applied",
        target_type="customer",
        target_id=customer_id,
        metadata={
            **_loyalty_order_audit_metadata(
                body=body,
                order=order,
                customer_id=customer_id,
                staff_user_id=staff_user_id,
            ),
            "reward_id": reward.id,
            "reward_name": reward.name,
            "reward_type": reward.reward_type,
            "points_required": reward.points_required,
            "discount_amount": str(discount_amount),
        },
    )


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


async def _resolve_loyalty_reward_discount(
    session: AsyncSession,
    user_id: int | None,
    reward_id: int | None,
    amount_eligible: float,
    resolved_items: list[tuple],
    order_type: str = "delivery",
) -> tuple[float, LoyaltyReward | None]:
    if reward_id is None:
        return 0.0, None
    if user_id is None:
        raise AppError("LOYALTY_USER_REQUIRED", "loyalty_customer_id is required", 422, "loyalty_customer_id")

    reward = await session.get(LoyaltyReward, reward_id)
    if reward is None or not reward.is_active:
        raise AppError("REWARD_NOT_FOUND", "Recompense introuvable ou inactive", 404, "loyalty_reward_id")

    account = await get_or_create_account(session, user_id, commit=False)
    if account.points < reward.points_required:
        raise AppError("INSUFFICIENT_POINTS", "Solde de points insuffisant", 422, "loyalty_reward_id")

    eligible = _money(amount_eligible)
    if reward.reward_type == "discount_euros":
        if reward.discount_amount is None:
            raise AppError("INVALID_REWARD", "Recompense mal configuree", 422, "loyalty_reward_id")
        return min(_money(reward.discount_amount), eligible), reward

    if reward.reward_type == "free_product":
        if reward.product_id is None:
            raise AppError("INVALID_REWARD", "Recompense mal configuree", 422, "loyalty_reward_id")
        matching_unit_prices = [
            _money(unit_price)
            for item, unit_price, *_rest in resolved_items
            if item.product_id == reward.product_id and item.quantity > 0
        ]
        if not matching_unit_prices:
            raise AppError(
                "REWARD_PRODUCT_REQUIRED",
                "Le produit offert doit etre present dans la commande",
                422,
                "loyalty_reward_id",
            )
        return min(min(matching_unit_prices), eligible), reward

    if reward.reward_type == "free_delivery":
        # Aucune remise sur les produits : la livraison offerte est appliquee par le moteur
        # de frais (create_order), qui debite aussi les points de la recompense.
        if order_type != "delivery":
            raise AppError(
                "REWARD_DELIVERY_REQUIRED",
                "Cette recompense ne s'applique qu'a une commande en livraison",
                422,
                "loyalty_reward_id",
            )
        return 0.0, reward

    raise AppError("INVALID_REWARD", "Type de recompense non supporte", 422, "loyalty_reward_id")


def _extras_from_snapshot(snapshot) -> list[dict]:
    return list(snapshot or [])


def _serialize_order_list(order: Order) -> dict:
    return {
        "id": order.id,
        "customer_email": order.customer_email,
        "customer_name": getattr(order, "customer_name", None),
        "customer_phone": getattr(order, "customer_phone", None),
        "establishment_id": getattr(order, "establishment_id", None),
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
        "delivery_lat": getattr(order, "delivery_lat", None),
        "delivery_lng": getattr(order, "delivery_lng", None),
        "delivery_instructions": getattr(order, "delivery_instructions", None),
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


async def _resolve_establishment_id(session: AsyncSession, establishment_id: int | None) -> int | None:
    if establishment_id is not None:
        existing_id = await session.scalar(
            select(Establishment.id).where(
                Establishment.id == establishment_id,
                Establishment.is_active.is_(True),
            )
        )
        if existing_id is None:
            raise AppError("ESTABLISHMENT_NOT_FOUND", "Establishment not found or inactive", 404, "establishment_id")
        return int(existing_id)

    default_id = await session.scalar(
        select(Establishment.id)
        .where(Establishment.is_active.is_(True))
        .order_by(Establishment.id)
        .limit(1)
    )
    if default_id is None:
        raise AppError("ESTABLISHMENT_REQUIRED", "No active establishment available for this order", 409)
    return int(default_id)


async def _resolve_delivery(
    session: AsyncSession,
    order_type: str,
    delivery_zone_id: int | None,
    subtotal: float,
    delivery_lat: float | None = None,
    delivery_lng: float | None = None,
    source: str = "customer",
    establishment_id: int | None = None,
    promo_free: bool = False,
    loyalty_free: bool = False,
) -> tuple[float, int, int | None, int | None]:
    """Calcule ``(frais, delai de trajet, zone effective, etablissement de la zone)``.

    Pour ``pickup`` / ``dine_in`` : aucun frais, aucun delai, ``delivery_zone_id`` ignore.

    Pour une livraison, le serveur est la seule autorite sur la zone et les frais :

    - la livraison doit etre activee (``DELIVERY_DISABLED`` 409 sinon) ;
    - avec des coordonnees GPS : la zone est retrouvee a partir du point, parmi les zones de
      l'etablissement demande (ou de tous si aucun) ; ``delivery_zone_id`` envoye par le client
      est ignore ; hors zone => 422 ``DELIVERY_ZONE_UNREACHABLE`` ;
    - sans coordonnees : refuse pour une commande client (422 ``DELIVERY_COORDINATES_REQUIRED``).
      Une commande saisie au comptoir (``source="manual"``) peut designer une zone choisie par le
      staff ; a defaut, 422 ``DELIVERY_ZONE_REQUIRED``.

    Les frais viennent de ``delivery.service.quote_for_zone`` : le meme calcul que
    ``POST /delivery/check``, pour que ce qui est annonce soit ce qui est facture (regles de zone
    horaires, livraison offerte des X EUR, code promo ou recompense « livraison offerte »). Le seuil
    est evalue sur le sous-total avant remises, comme dans ``/delivery/check``.
    """
    if order_type in NON_DELIVERY_ORDER_TYPES:
        return 0.0, 0, None, None

    if not await delivery_service.is_delivery_enabled(session):
        raise AppError("DELIVERY_DISABLED", "La livraison est momentanement indisponible", 409)

    if delivery_lat is not None and delivery_lng is not None:
        zone = await delivery_service.check_address(session, delivery_lat, delivery_lng, establishment_id)
    elif source == "manual":
        if delivery_zone_id is None:
            raise AppError(
                "DELIVERY_ZONE_REQUIRED",
                "Une zone de livraison est requise pour une commande en livraison",
                422,
                "delivery_zone_id",
            )
        zone = await session.get(DeliveryZone, delivery_zone_id)
        out_of_scope = (
            zone is not None
            and establishment_id is not None
            and zone.establishment_id is not None
            and zone.establishment_id != establishment_id
        )
        if out_of_scope:
            raise AppError(
                "INVALID_DELIVERY_ZONE",
                "Cette zone n'appartient pas a l'etablissement de la commande",
                422,
                "delivery_zone_id",
            )
    else:
        raise AppError(
            "DELIVERY_COORDINATES_REQUIRED",
            "Les coordonnees GPS de livraison sont requises",
            422,
            "delivery_lat",
        )

    if zone is None or not zone.is_active:
        raise AppError("INVALID_DELIVERY_ZONE", "Delivery zone not found or inactive", 422, "delivery_zone_id")

    quote = await delivery_service.quote_for_zone(
        session, zone, subtotal, promo_free=promo_free, loyalty_free=loyalty_free
    )
    if quote.min_order_met is False:
        raise AppError(
            "DELIVERY_MIN_ORDER_NOT_MET",
            f"Order subtotal is below the delivery zone minimum ({quote.min_order_amount:.2f})",
            422,
            "delivery_zone_id",
        )
    return _money(quote.pricing.fee), quote.estimated_minutes, zone.id, quote.establishment_id


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
    promo_free_delivery = False
    if body.promo_code:
        promo_flags = (
            await session.execute(
                select(Promotion.free_delivery, Promotion.discount_value).where(
                    Promotion.code == body.promo_code.strip().upper()
                )
            )
        ).first()
        if promo_flags is not None and bool(promo_flags[0]):
            if (getattr(body, "order_type", None) or "delivery") != "delivery" and float(promo_flags[1]) == 0:
                # Un code « livraison offerte » seul n'a aucun effet hors livraison : on le
                # refuse plutot que de consommer silencieusement une utilisation.
                raise AppError(
                    "PROMO_DELIVERY_ONLY",
                    "Ce code ne s'applique qu'a une commande en livraison",
                    422,
                    "promo_code",
                )
            promo_free_delivery = True
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
    loyalty_reward_id = getattr(body, "loyalty_reward_id", None)
    loyalty_reward: LoyaltyReward | None = None
    loyalty_discount = 0.0
    if loyalty_points_to_use and loyalty_reward_id:
        raise AppError(
            "LOYALTY_REWARD_CONFLICT",
            "Une seule recompense fidelite peut etre appliquee",
            422,
            "loyalty_reward_id",
        )
    if loyalty_reward_id:
        loyalty_discount, loyalty_reward = await _resolve_loyalty_reward_discount(
            session,
            user_id,
            loyalty_reward_id,
            max(0.0, subtotal - discount_total),
            resolved_items,
            order_type=getattr(body, "order_type", None) or "delivery",
        )
        discount_total = _money(discount_total + loyalty_discount)
    if loyalty_points_to_use:
        loyalty_discount, loyalty_points_to_use = await _resolve_loyalty_discount(
            session,
            user_id,
            loyalty_points_to_use,
            max(0.0, subtotal - discount_total),
        )
        discount_total = _money(discount_total + loyalty_discount)

    order_type = getattr(body, "order_type", None) or "delivery"
    customer_name = customer_name or getattr(body, "customer_name", None)
    customer_phone = customer_phone or getattr(body, "customer_phone", None)
    if order_type == "delivery" and not customer_phone:
        # Le livreur doit pouvoir joindre le client (adresse introuvable, absence).
        raise AppError(
            "CUSTOMER_PHONE_REQUIRED",
            "Un telephone est requis pour une commande en livraison",
            422,
            "customer_phone",
        )
    # Une livraison est rattachee a l'etablissement dont la zone couvre le point ; on resout
    # l'etablissement demande AVANT pour restreindre la recherche de zone. Pickup / sur place :
    # l'ordre des requetes reste inchange (l'etablissement est resolu plus bas).
    establishment_id: int | None = None
    requested_establishment_id = getattr(body, "establishment_id", None)
    if order_type == "delivery" and requested_establishment_id is not None:
        establishment_id = await _resolve_establishment_id(session, requested_establishment_id)
    delivery_fee, delivery_minutes, effective_delivery_zone_id, zone_establishment_id = await _resolve_delivery(
        session,
        order_type,
        getattr(body, "delivery_zone_id", None),
        subtotal,
        delivery_lat=getattr(body, "delivery_lat", None),
        delivery_lng=getattr(body, "delivery_lng", None),
        source=source,
        establishment_id=establishment_id,
        promo_free=promo_free_delivery,
        loyalty_free=loyalty_reward is not None and loyalty_reward.reward_type == "free_delivery",
    )
    if establishment_id is None and zone_establishment_id is not None:
        establishment_id = zone_establishment_id
    estimated_delivery_at = await _estimate_delivery_at(session, delivery_minutes)
    if establishment_id is None:
        establishment_id = await _resolve_establishment_id(session, requested_establishment_id)

    total = _money(subtotal - discount_total + delivery_fee)
    order = Order(
        user_id=user_id,
        customer_email=body.customer_email,
        customer_name=customer_name,
        customer_phone=customer_phone,
        establishment_id=establishment_id,
        order_type=order_type,
        source=source,
        created_by_user_id=created_by_user_id,
        delivery_address=body.delivery_address if order_type == "delivery" else None,
        subtotal=subtotal,
        discount_total=discount_total,
        delivery_fee=delivery_fee,
        delivery_zone_id=effective_delivery_zone_id,
        delivery_lat=getattr(body, "delivery_lat", None) if order_type == "delivery" else None,
        delivery_lng=getattr(body, "delivery_lng", None) if order_type == "delivery" else None,
        delivery_instructions=(
            getattr(body, "delivery_instructions", None) if order_type == "delivery" else None
        ),
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
        loyalty_metadata = _loyalty_order_audit_metadata(
            body=body,
            order=order,
            customer_id=user_id,
            staff_user_id=created_by_user_id,
        )
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
                metadata_json={**loyalty_metadata, "discount_amount": str(loyalty_discount)},
            )
        )
    if loyalty_reward is not None:
        if user_id is None:
            raise AppError("LOYALTY_USER_REQUIRED", "loyalty_customer_id is required", 422, "loyalty_customer_id")
        account = await get_or_create_account(session, user_id, commit=False)
        loyalty_metadata = _loyalty_order_audit_metadata(
            body=body,
            order=order,
            customer_id=user_id,
            staff_user_id=created_by_user_id,
        )
        account.points -= loyalty_reward.points_required
        session.add(
            LoyaltyTransaction(
                account_id=account.id,
                points_delta=-loyalty_reward.points_required,
                reason=f"manual_reward_{loyalty_reward.id}_order_{order.id}",
                transaction_type="redeem",
                source="staff_checkout",
                changed_by_user_id=created_by_user_id,
                order_id=order.id,
                reward_id=loyalty_reward.id,
                metadata_json={
                    **loyalty_metadata,
                    "reward_name": loyalty_reward.name,
                    "reward_type": loyalty_reward.reward_type,
                    "discount_amount": str(loyalty_discount),
                },
            )
        )
        await _record_loyalty_reward_audit(
            session,
            body=body,
            order=order,
            customer_id=user_id,
            staff_user_id=created_by_user_id,
            reward=loyalty_reward,
            discount_amount=loyalty_discount,
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


async def get_delivery_code(
    session: AsyncSession, order_id: int, user_id: int, tenant_slug: str
) -> dict:
    """Code de remise du client proprietaire de la commande (a donner au livreur). Jamais expose
    au personnel ni au livreur par cette route ; 404 pour la commande d'un autre."""
    order = await session.get(Order, order_id)
    if order is None or order.user_id is None or int(order.user_id) != int(user_id):
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.order_type != "delivery":
        raise AppError("ORDER_NOT_DELIVERY", "Cette commande n'est pas une livraison.", 404)
    if order.status not in {"confirmed", "queued", "preparing", "ready", "out_for_delivery"}:
        raise AppError("ORDER_NOT_ACTIVE", "Aucun code a donner pour cette commande.", 409)
    code = await delivery_proof_service.get_code(session, tenant_slug, order_id)
    return {"code": code, "length": delivery_proof_service.CODE_LENGTH}


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
        awaiting_link = existing_order.status == "pending" and (
            payment is None or payment.status == "pending"
        )
        if payment is None and not awaiting_link:
            raise AppError("PAYMENT_NOT_FOUND", "Manual order payment not found", 409)
        link = None
        if awaiting_link:
            from app.modules.payments import guarantee as _guarantee

            link = await _guarantee.get_payment_link(session, tenant_slug, existing_order.id)
        return {
            "order": await _serialize_order_detail(session, existing_order),
            "payment": _payment_payload(payment) if payment is not None and not awaiting_link else None,
            "receipt": await build_receipt(session, existing_order.id),
            "payment_link": link,
        }

    payment_body = body.payment
    if payment_body.method not in MANUAL_PAYMENT_PROVIDERS | {"payment_link"}:
        raise AppError("INVALID_PAYMENT_METHOD", "Unsupported manual payment method", 422, "payment.method")
    is_delivery = getattr(body, "order_type", "delivery") == "delivery"
    if is_delivery and payment_body.method == "cash":
        # Especes non encore encaissees : on ne peut pas declarer la commande payee. Le client
        # laisse une empreinte bancaire (lien) puis paie a la remise.
        raise AppError(
            "DELIVERY_CASH_REQUIRES_GUARANTEE",
            "Une livraison payee en especes exige une empreinte bancaire : utilisez un lien de paiement.",
            422,
            "payment.method",
        )
    if payment_body.method == "payment_link" and getattr(body, "order_type", "delivery") == "dine_in":
        raise AppError(
            "PAYMENT_LINK_UNSUPPORTED",
            "Lien de paiement indisponible pour une commande sur place.",
            422,
            "payment.method",
        )
    link_mode = payment_body.link_mode or ("guarantee" if is_delivery else "full")
    if payment_body.method == "payment_link" and link_mode == "guarantee" and not is_delivery:
        raise AppError(
            "GUARANTEE_DELIVERY_ONLY", "L'empreinte ne concerne que les livraisons.", 422, "payment.link_mode"
        )

    customer = body.customer
    loyalty_customer_id = getattr(body, "loyalty_customer_id", None) or getattr(body, "loyalty_user_id", None)
    try:
        order = await create_order(
            session,
            body,
            user_id=loyalty_customer_id,
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
        if payment_body.method == "payment_link":
            # Commande creee « en attente » : elle est confirmee par le webhook Stripe quand le
            # client valide le lien (paiement ou empreinte).
            payment = None
            await session.commit()
        else:
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

    payment_link = None
    payment_link_error = None
    if payment_body.method == "payment_link":
        from app.modules.payments import guarantee as _guarantee

        try:
            payment_link = await _guarantee.create_payment_link(
                session,
                tenant_slug,
                order.id,
                mode=link_mode,
                actor_user_id=actor_user_id,
                arq_pool=arq_pool,
            )
        except AppError as exc:
            payment_link_error = exc.code
        except Exception:
            logger.exception("payment link creation failed for manual order_id=%s", order.id)
            payment_link_error = "PAYMENT_LINK_FAILED"

    if order.user_id is not None and order.payment_status == "paid" and order.status in {"confirmed", "queued"}:
        try:
            await _credit_loyalty_for_order(
                session,
                order,
                staff_user_id=actor_user_id,
                identification_method=getattr(body, "loyalty_identification_method", None),
                oral_confirmed=bool(getattr(body, "loyalty_oral_confirmed", False)),
            )
        except Exception as exc:
            logger.error(
                "loyalty.credit_points_for_order failed for manual order_id=%s user_id=%s: %s",
                order.id,
                order.user_id,
                exc,
            )

    await session.refresh(order)
    if payment is not None:
        await session.refresh(payment)
    return {
        "order": await _serialize_order_detail(session, order),
        "payment": _payment_payload(payment) if payment is not None else None,
        "receipt": await build_receipt(session, order.id),
        "payment_link": payment_link,
        "payment_link_error": payment_link_error,
    }


async def _credit_loyalty_for_order(
    session: AsyncSession,
    order: Order,
    *,
    staff_user_id: int | None = None,
    identification_method: str | None = None,
    oral_confirmed: bool = False,
) -> None:
    if order.user_id is None:
        return

    items_result = await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
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
        order.id,
        float(order.total),
        category_ids,
        metadata={
            "order_id": order.id,
            "establishment_id": order.establishment_id,
            "customer_id": order.user_id,
            "staff_user_id": staff_user_id,
            "loyalty_identification_method": identification_method,
            "loyalty_oral_confirmed": oral_confirmed,
        },
    )


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
                title=t("Order ready") if order.order_type == "delivery" else t("Ready for pickup"),
                body=(
                    t(
                        "Your order #{order_id} is ready and will leave with a driver shortly.",
                        order_id=order_id,
                    )
                    if order.order_type == "delivery"
                    else t("Your order #{order_id} is ready!", order_id=order_id)
                ),
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
    establishment_id: int | None = None,
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
    if establishment_id is not None:
        filters.append(Order.establishment_id == establishment_id)

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
        raise AppError("ORDER_NOT_FOUND", t("Order not found"), 404)
    if not is_staff and order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", t("Order not found"), 404)
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
        raise AppError("ORDER_NOT_FOUND", t("Order not found"), 404)
    if order.status != "pending":
        raise AppError("ORDER_CANCEL_NOT_ALLOWED", t("Only pending orders can be cancelled by customer"), 422)
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
        raise AppError("ORDER_NOT_FOUND", t("Order not found"), 404)
    if not is_staff and order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", t("Order not found"), 404)

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
    delivery_proof: str | None = None,
    allow_redelivery: bool = False,
) -> Order:
    """Met a jour le statut d'une commande avec notifications temps reel.

    - Transition vers "confirmed" : deduit le stock atomiquement dans la meme transaction.
    - Transition vers "cancelled" depuis "confirmed" : restitue le stock deduit
      (restore_for_order) dans la meme transaction avant commit.
    - Transition vers "cancelled" depuis "pending" : pas de restitution (stock non deduit).
    - Post-commit : envoie les notifications WebSocket et push selon la transition.

    [PROD] Ne jamais appeler session.commit() avant cette fonction dans le
    meme contexte de session : cela briserait l'atomicite stock/statut.

    ``delivery_proof`` : comment la remise a ete prouvee (``code``, ``admin_override``,
    ``not_required``) ; quand la preuve est exigee, une livraison avec livreur ne peut passer
    ``delivered`` sans elle. ``allow_redelivery`` : autorise ``delivery_failed -> ready`` (seul
    le traitement admin d'un echec l'utilise).

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
    # [CONCURRENCE] Verrou de ligne : deux appareils (ex. comptoir + livreur) qui valident
    # la meme commande en meme temps sont serialises. Le second voit le nouveau statut
    # et est rejete (INVALID_STATUS_TRANSITION) au lieu de dupliquer historique,
    # deduction de stock ou credit de points. populate_existing evite de lire un etat
    # perime depuis l'identity map de la session.
    order = await session.get(Order, order_id, with_for_update=True, populate_existing=True)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    previous_status = order.status

    if (
        status == "delivery_failed"
        and authority == TransitionAuthority.INTERNAL
        and not (note or "").strip()
    ):
        raise AppError(
            "DELIVERY_FAILURE_REASON_REQUIRED",
            "Un motif est requis pour declarer un echec de livraison",
            422,
            "note",
        )

    current_payment_status = getattr(order, "payment_status", None) or "pending"
    # « guaranteed » : livraison payee a la remise, carte pre-autorisee (voir payments/guarantee.py).
    if status == "confirmed" and not (
        current_payment_status == "paid"
        or (current_payment_status == "guaranteed" and order.order_type == "delivery")
    ):
        raise AppError("PAYMENT_REQUIRED", "Order must be paid before confirmation", 409, "payment_status")
    if status == "out_for_delivery" and authority == TransitionAuthority.INTERNAL:
        # Dispatch actif : pas de depart sans livreur assigne (voir delivery/lifecycle.py).
        await delivery_lifecycle.ensure_driver_for_departure(session, order)
    if (
        status == "delivered"
        and delivery_proof is None
        and authority == TransitionAuthority.INTERNAL
        and await delivery_lifecycle.proof_required_for(session, order)
    ):
        raise AppError(
            "DELIVERY_PROOF_REQUIRED",
            "Cette livraison se conclut avec le code du client (ou, pour un administrateur, sans code avec un motif).",
            409,
            "status",
        )
    if status == "delivered" and current_payment_status == "guaranteed":
        raise AppError(
            "PAYMENT_SETTLEMENT_REQUIRED",
            "Enregistrez le reglement (especes ou carte) avant de marquer la commande livree.",
            409,
            "payment_status",
        )

    # Validate the transition using the ORIGINAL requested status before any redirect.
    is_graph_transition = status in VALID_TRANSITIONS.get(previous_status, set()) or (
        allow_redelivery and previous_status == "delivery_failed" and status == "ready"
    )
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

    # La livraison suit la commande dans la meme transaction : elle ne peut pas diverger.
    if actual_status == "out_for_delivery":
        await delivery_lifecycle.on_departure(session, order, actor_user_id)
    elif actual_status == "delivered":
        await delivery_lifecycle.on_delivered(session, order, actor_user_id, delivery_proof)
    elif actual_status in {"cancelled", "rejected", "delivery_failed"}:
        await delivery_lifecycle.on_order_closed(session, order, actual_status, actor_user_id)

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

    # Empreinte bancaire : une commande annulee ou rejetee libere la pre-autorisation (jamais
    # bloquant : en cas d'echec Stripe, l'empreinte expire d'elle-meme au bout de 7 jours).
    if actual_status in {"cancelled", "rejected"} and (
        getattr(order, "payment_status", None) == "guaranteed"
    ):
        from app.modules.payments import guarantee as _guarantee

        await _guarantee.release_guarantee(
            session,
            tenant_slug or "default",
            order_id,
            reason=f"order_{actual_status}",
            user_id=actor_user_id,
            best_effort=True,
        )
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
    # [i18n] Traduit AVANT l'enqueue : le worker arq tourne dans un process
    # separe, le ContextVar de locale de cette requete n'y survit pas.
    if actual_status == "cancelled" and arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_email",
                to=order.customer_email or "",
                subject=t("Your order has been cancelled"),
                body=t("Your order #{order_id} has been cancelled.", order_id=order_id),
            )
        except Exception:
            pass

    # Credite les points de fidelite apres livraison confirmee.
    # Recupere les category_ids des produits commandes pour le calcul des regles bonus.
    if actual_status == "delivered" and order.user_id is not None:
        try:
            await _credit_loyalty_for_order(session, order)
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
        # [i18n] client_title/client_body traduits (t()) -- perimetre client
        # uniquement. staff_title/staff_body restent en francais (hors perimetre,
        # voir plan devise+locale).
        # Code de remise : donne au client des qu'il part (push), ou par SMS s'il n'a pas de compte.
        _delivery_code: str | None = None
        if actual_status == "out_for_delivery" and order.order_type == "delivery":
            try:
                _delivery_code = await delivery_proof_service.get_code(session, _effective_tenant, order_id)
            except Exception as exc:
                logger.error("delivery code unavailable for order_id=%s: %s", order_id, exc)
        _notif_map: dict[tuple[str, str], dict] = {
            ("pending", "confirmed"): {
                "client_title": t("Order confirmed"),
                "client_body": t("Your order #{order_id} has been confirmed.", order_id=order_id),
                "staff_title": "Nouvelle commande",
                "staff_body": f"Nouvelle commande #{order_id} recue.",
            },
            ("confirmed", "preparing"): {
                "client_title": t("Being prepared"),
                "client_body": t("Your order #{order_id} is being prepared.", order_id=order_id),
                "staff_title": None,
                "staff_body": None,
            },
            ("preparing", "ready"): (
                {
                    "client_title": t("Order ready"),
                    "client_body": t(
                        "Your order #{order_id} is ready and will leave with a driver shortly.",
                        order_id=order_id,
                    ),
                    "staff_title": None,
                    "staff_body": None,
                }
                if order.order_type == "delivery"
                else {
                    "client_title": t("Ready for pickup"),
                    "client_body": t("Your order #{order_id} is ready!", order_id=order_id),
                    "staff_title": None,
                    "staff_body": None,
                }
            ),
            ("ready", "out_for_delivery"): {
                "client_title": t("Your driver is on the way"),
                "client_body": (
                    t(
                        "Your order #{order_id} has left the restaurant. Your delivery code: {code}",
                        order_id=order_id,
                        code=_delivery_code,
                    )
                    if _delivery_code
                    else t("Your order #{order_id} has left the restaurant.", order_id=order_id)
                ),
                "staff_title": None,
                "staff_body": None,
            },
            ("ready", "delivered"): {
                "client_title": t("Delivered! Enjoy your meal"),
                "client_body": t("Your order #{order_id} has been delivered. Enjoy!", order_id=order_id),
                "staff_title": None,
                "staff_body": None,
            },
            ("out_for_delivery", "delivered"): {
                "client_title": t("Delivered! Enjoy your meal"),
                "client_body": t("Your order #{order_id} has been delivered. Enjoy!", order_id=order_id),
                "staff_title": None,
                "staff_body": None,
            },
        }

        # Echec de livraison : le client est prevenu, le staff doit statuer (remboursement
        # ou nouvelle tentative). [i18n] staff_* restent en francais, comme ci-dessus.
        if actual_status == "delivery_failed":
            notif: dict | None = {
                "client_title": t("Delivery failed"),
                "client_body": t(
                    "We could not deliver your order #{order_id}. The restaurant will contact you.",
                    order_id=order_id,
                ),
                "staff_title": "Echec de livraison",
                "staff_body": f"Commande #{order_id} : livraison echouee, a traiter (remboursement ou nouvelle tentative).",
            }
        # Transitions vers "cancelled" depuis n'importe quel etat.
        elif actual_status == "cancelled":
            notif = {
                "client_title": t("Order cancelled"),
                "client_body": t("Your order #{order_id} has been cancelled.", order_id=order_id),
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

            # Client sans compte (commande saisie au comptoir) : le code part par SMS.
            if (
                _delivery_code
                and order.user_id is None
                and order.customer_phone
                and arq_pool is not None
            ):
                try:
                    from app.core.sms.service import enqueue_sms
                    from app.modules.customer.service import normalize_phone_e164

                    await enqueue_sms(
                        arq_pool,
                        to_phone_e164=normalize_phone_e164(order.customer_phone),
                        body=t(
                            "Order #{order_id} is on its way. Give this code to the driver: {code}",
                            order_id=order_id,
                            code=_delivery_code,
                        ),
                    )
                except Exception as exc:
                    logger.warning("delivery code SMS not sent for order_id=%s: %s", order_id, exc)

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
