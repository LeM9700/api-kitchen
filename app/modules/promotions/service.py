"""Service de gestion des codes promo.

Les erreurs de validation exposees au client restent volontairement generiques
pour eviter l'enumeration de codes et les fuites sur quotas, dates ou ciblage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import string
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.orders.models import Order
from app.modules.promotions.models import (
    PromoCodeUsage,
    Promotion,
    PromotionCampaign,
    PromotionTargetCategory,
    PromotionTargetProduct,
)
from app.modules.promotions.schemas import (
    PromotionAdminOut,
    PromotionCampaignCreate,
    PromotionCampaignOut,
    PromotionCartItem,
    PromotionCleanupOut,
    PromotionCreate,
    PromotionOut,
    PromotionPublicOut,
    PromotionSuperAdminStatsOut,
    PromotionTargets,
    PromotionUpdate,
    PromotionUsageOut,
    PromotionValidateOut,
)

INVALID_PROMO_DETAIL = "Code invalide ou expire"
CODE_ALPHABET = string.ascii_uppercase + string.digits


@dataclass(frozen=True)
class PromotionStats:
    usage_count: int = 0
    unique_users: int = 0
    revenue_gross: float = 0
    revenue_net: float = 0
    discount_total: float = 0


def _invalid_promo() -> AppError:
    return AppError("INVALID_PROMO", INVALID_PROMO_DETAIL, 422, "promo_code")


def _money(value: Any) -> float:
    return round(float(value or 0), 2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _targeted_total(
    items: list[PromotionCartItem],
    targets: PromotionTargets,
    fallback_total: float,
) -> float:
    has_targets = bool(targets.category_ids or targets.product_ids)
    if not has_targets:
        if items:
            return _money(sum(item.total for item in items))
        return _money(fallback_total)

    eligible = 0.0
    category_ids = set(targets.category_ids)
    product_ids = set(targets.product_ids)
    for item in items:
        if item.product_id is not None and item.product_id in product_ids:
            eligible += item.total
            continue
        if item.category_id is not None and item.category_id in category_ids:
            eligible += item.total
    return _money(eligible)


async def _targets_for_promo(session: AsyncSession, promo_id: int) -> PromotionTargets:
    category_rows = await session.execute(
        select(PromotionTargetCategory.category_id).where(PromotionTargetCategory.promotion_id == promo_id)
    )
    product_rows = await session.execute(
        select(PromotionTargetProduct.product_id).where(PromotionTargetProduct.promotion_id == promo_id)
    )
    return PromotionTargets(
        category_ids=list(category_rows.scalars().all()),
        product_ids=list(product_rows.scalars().all()),
    )


async def _set_targets(
    session: AsyncSession,
    promo_id: int,
    category_ids: list[int] | None,
    product_ids: list[int] | None,
) -> None:
    if category_ids is not None:
        await session.execute(
            delete(PromotionTargetCategory).where(PromotionTargetCategory.promotion_id == promo_id)
        )
        for category_id in sorted(set(category_ids)):
            session.add(PromotionTargetCategory(promotion_id=promo_id, category_id=category_id))
    if product_ids is not None:
        await session.execute(delete(PromotionTargetProduct).where(PromotionTargetProduct.promotion_id == promo_id))
        for product_id in sorted(set(product_ids)):
            session.add(PromotionTargetProduct(promotion_id=promo_id, product_id=product_id))


async def _email_is_verified(session: AsyncSession, user_id: int | None) -> bool:
    if user_id is None:
        return False
    from app.modules.auth.models import User

    verified_at = await session.scalar(select(User.email_verified_at).where(User.id == user_id))
    return verified_at is not None


async def _user_has_delivered_order(session: AsyncSession, user_id: int) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.user_id == user_id, Order.status == "delivered")
    )
    return (count or 0) > 0


async def _assert_promo_eligible(
    session: AsyncSession,
    promo: Promotion,
    order_total: float,
    user_id: int | None,
    email_verified: bool | None,
    check_global_quota: bool = True,
) -> None:
    if user_id is None:
        raise _invalid_promo()

    now = _now()
    if not promo.is_active:
        raise _invalid_promo()
    if promo.starts_at and promo.starts_at > now:
        raise _invalid_promo()
    if promo.ends_at and promo.ends_at < now:
        raise _invalid_promo()
    if promo.user_id is not None and int(promo.user_id) != int(user_id):
        raise _invalid_promo()
    if check_global_quota and promo.max_uses is not None and promo.current_uses >= promo.max_uses:
        raise _invalid_promo()
    if order_total < float(promo.min_order_amount):
        raise _invalid_promo()
    if promo.email_verified_required:
        verified = email_verified if email_verified is not None else await _email_is_verified(session, user_id)
        if not verified:
            raise _invalid_promo()

    if promo.max_uses_per_user is not None:
        user_usage_count = await session.scalar(
            select(func.count(PromoCodeUsage.id)).where(
                PromoCodeUsage.promo_code_id == promo.id,
                PromoCodeUsage.user_id == user_id,
            )
        )
        if (user_usage_count or 0) >= promo.max_uses_per_user:
            raise _invalid_promo()

    if promo.first_order_only and await _user_has_delivered_order(session, user_id):
        raise _invalid_promo()


def _discount_for_promo(promo: Promotion, eligible_total: float) -> float:
    if eligible_total <= 0:
        raise _invalid_promo()
    if promo.discount_type == "percent":
        discount = eligible_total * float(promo.discount_value) / 100
    else:
        discount = float(promo.discount_value)
    return _money(min(discount, eligible_total))


async def _load_promos(session: AsyncSession, codes: list[str]) -> list[Promotion]:
    if not codes:
        raise _invalid_promo()
    normalized = [code.strip().upper() for code in codes if code.strip()]
    if len(normalized) != len(set(normalized)):
        raise _invalid_promo()
    result = await session.execute(select(Promotion).where(Promotion.code.in_(normalized)))
    promos_by_code = {promo.code: promo for promo in result.scalars().all()}
    try:
        return [promos_by_code[code] for code in normalized]
    except KeyError as exc:
        raise _invalid_promo() from exc


async def _calculate_discounts(
    session: AsyncSession,
    promos: list[Promotion],
    order_total: float,
    items: list[PromotionCartItem],
    user_id: int | None,
    email_verified: bool | None = None,
    check_global_quota: bool = True,
) -> tuple[float, dict[str, float], dict[int, PromotionTargets]]:
    if len(promos) > 1 and any(not promo.is_stackable for promo in promos):
        raise _invalid_promo()

    targets_by_id: dict[int, PromotionTargets] = {}
    for promo in promos:
        await _assert_promo_eligible(
            session,
            promo,
            order_total,
            user_id,
            email_verified,
            check_global_quota=check_global_quota,
        )
        targets_by_id[promo.id] = await _targets_for_promo(session, promo.id)

    ordered = sorted(promos, key=lambda promo: 0 if promo.discount_type == "percent" else 1)
    remaining_order_total = _money(order_total)
    discounts: dict[str, float] = {}

    for promo in ordered:
        eligible_total = _targeted_total(items, targets_by_id[promo.id], order_total)
        raw_discount = _discount_for_promo(promo, eligible_total)
        discount = _money(min(raw_discount, remaining_order_total))
        if discount <= 0:
            raise _invalid_promo()
        discounts[promo.code] = discount
        remaining_order_total = _money(remaining_order_total - discount)

    total_discount = _money(sum(discounts.values()))
    return total_discount, discounts, targets_by_id


def _promotion_out(promo: Promotion, targets: PromotionTargets) -> PromotionOut:
    return PromotionOut(
        id=promo.id,
        code=promo.code,
        description=promo.description,
        discount_type=promo.discount_type,
        discount_value=float(promo.discount_value),
        min_order_amount=float(promo.min_order_amount),
        starts_at=promo.starts_at,
        ends_at=promo.ends_at,
        is_active=promo.is_active,
        max_uses=promo.max_uses,
        max_uses_per_user=promo.max_uses_per_user,
        current_uses=promo.current_uses,
        first_order_only=promo.first_order_only,
        campaign_id=promo.campaign_id,
        user_id=promo.user_id,
        is_public=promo.is_public,
        is_stackable=promo.is_stackable,
        email_verified_required=promo.email_verified_required,
        targets=targets,
    )


def _public_out(promo: Promotion, targets: PromotionTargets) -> PromotionPublicOut:
    return PromotionPublicOut(
        id=promo.id,
        code=promo.code,
        description=promo.description,
        discount_type=promo.discount_type,
        discount_value=float(promo.discount_value),
        min_order_amount=float(promo.min_order_amount),
        starts_at=promo.starts_at,
        ends_at=promo.ends_at,
        targets=targets,
    )


def _admin_out(promo: Promotion, targets: PromotionTargets, stats: PromotionStats) -> PromotionAdminOut:
    remaining_uses = None
    if promo.max_uses is not None:
        remaining_uses = max(0, int(promo.max_uses) - int(promo.current_uses))
    base = _promotion_out(promo, targets)
    return PromotionAdminOut(
        **base.model_dump(),
        usage_count=stats.usage_count,
        unique_users=stats.unique_users,
        remaining_uses=remaining_uses,
        revenue_gross=stats.revenue_gross,
        revenue_net=stats.revenue_net,
        discount_total=stats.discount_total,
    )


async def _stats_for_promo(session: AsyncSession, promo_id: int) -> PromotionStats:
    row = await session.execute(
        select(
            func.count(PromoCodeUsage.id),
            func.count(func.distinct(PromoCodeUsage.user_id)),
            func.coalesce(func.sum(Order.subtotal), 0),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.discount_total), 0),
        )
        .select_from(PromoCodeUsage)
        .join(Order, Order.id == PromoCodeUsage.order_id, isouter=True)
        .where(PromoCodeUsage.promo_code_id == promo_id)
    )
    usage_count, unique_users, gross, net, discount = row.one()
    return PromotionStats(
        usage_count=int(usage_count or 0),
        unique_users=int(unique_users or 0),
        revenue_gross=_money(gross),
        revenue_net=_money(net),
        discount_total=_money(discount),
    )


async def create_promotion(session: AsyncSession, body: PromotionCreate) -> PromotionOut:
    data = body.model_dump(exclude={"target_category_ids", "target_product_ids"})
    promo = Promotion(**data)
    session.add(promo)
    await session.flush()
    await _set_targets(session, promo.id, body.target_category_ids, body.target_product_ids)
    await session.commit()
    await session.refresh(promo)
    return _promotion_out(promo, await _targets_for_promo(session, promo.id))


async def update_promotion(session: AsyncSession, promo_id: int, body: PromotionUpdate) -> PromotionOut:
    promo = await session.get(Promotion, promo_id)
    if promo is None:
        raise AppError("PROMOTION_NOT_FOUND", "Promotion not found", 404)

    data = body.model_dump(exclude_unset=True)
    category_ids = data.pop("target_category_ids", None)
    product_ids = data.pop("target_product_ids", None)
    for key, value in data.items():
        setattr(promo, key, value)
    await _set_targets(session, promo.id, category_ids, product_ids)
    await session.commit()
    await session.refresh(promo)
    return _promotion_out(promo, await _targets_for_promo(session, promo.id))


async def toggle_promotion(session: AsyncSession, promo_id: int, is_active: bool) -> PromotionOut:
    promo = await session.get(Promotion, promo_id)
    if promo is None:
        raise AppError("PROMOTION_NOT_FOUND", "Promotion not found", 404)
    promo.is_active = is_active
    await session.commit()
    await session.refresh(promo)
    return _promotion_out(promo, await _targets_for_promo(session, promo.id))


async def delete_promotion(session: AsyncSession, promo_id: int) -> None:
    promo = await session.get(Promotion, promo_id)
    if promo is not None:
        promo.is_active = False
        await session.commit()


async def get_promotion(session: AsyncSession, promo_id: int) -> PromotionAdminOut:
    promo = await session.get(Promotion, promo_id)
    if promo is None:
        raise AppError("PROMOTION_NOT_FOUND", "Promotion not found", 404)
    return _admin_out(promo, await _targets_for_promo(session, promo.id), await _stats_for_promo(session, promo.id))


async def list_active(session: AsyncSession) -> list[PromotionPublicOut]:
    now = _now()
    result = await session.execute(
        select(Promotion)
        .where(
            Promotion.is_active.is_(True),
            Promotion.is_public.is_(True),
            Promotion.user_id.is_(None),
            (Promotion.starts_at.is_(None) | (Promotion.starts_at <= now)),
            (Promotion.ends_at.is_(None) | (Promotion.ends_at >= now)),
        )
        .order_by(Promotion.code)
    )
    promos = list(result.scalars().all())
    return [_public_out(promo, await _targets_for_promo(session, promo.id)) for promo in promos]


async def list_admin(
    session: AsyncSession,
    pagination: PaginationParams,
    is_active: bool | None = None,
    campaign_id: int | None = None,
    code: str | None = None,
    user_id: int | None = None,
    is_public: bool | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> tuple[list[PromotionAdminOut], int]:
    def apply_filters(stmt):
        if is_active is not None:
            stmt = stmt.where(Promotion.is_active.is_(is_active))
        if campaign_id is not None:
            stmt = stmt.where(Promotion.campaign_id == campaign_id)
        if code:
            stmt = stmt.where(Promotion.code.ilike(f"%{code.strip().upper()}%"))
        if user_id is not None:
            stmt = stmt.where(Promotion.user_id == user_id)
        if is_public is not None:
            stmt = stmt.where(Promotion.is_public.is_(is_public))
        if starts_at is not None:
            stmt = stmt.where((Promotion.starts_at.is_(None)) | (Promotion.starts_at >= starts_at))
        if ends_at is not None:
            stmt = stmt.where((Promotion.ends_at.is_(None)) | (Promotion.ends_at <= ends_at))
        return stmt

    total = await session.scalar(apply_filters(select(func.count(Promotion.id)))) or 0
    result = await session.execute(
        apply_filters(select(Promotion))
        .order_by(Promotion.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    promos = list(result.scalars().all())
    items = [
        _admin_out(promo, await _targets_for_promo(session, promo.id), await _stats_for_promo(session, promo.id))
        for promo in promos
    ]
    return items, int(total)


async def preview_promos(
    session: AsyncSession,
    codes: list[str],
    order_total: float,
    items: list[PromotionCartItem],
    user_id: int | None,
    email_verified: bool | None = None,
) -> PromotionValidateOut:
    promos = await _load_promos(session, codes)
    discount, discounts, _targets = await _calculate_discounts(
        session,
        promos,
        order_total,
        items,
        user_id,
        email_verified=email_verified,
        check_global_quota=True,
    )
    return PromotionValidateOut(
        discount=discount,
        discounts=discounts,
        promo_id=promos[0].id if len(promos) == 1 else None,
        promo_ids=[promo.id for promo in promos],
    )


async def apply_promo(
    session: AsyncSession,
    tenant_slug: str,
    promo_code: str,
    subtotal: float,
    user_id: int | None = None,
    items: list[PromotionCartItem] | None = None,
    email_verified: bool | None = None,
) -> float:
    promos = await _load_promos(session, [promo_code])
    promo = promos[0]
    cart_items = items or []
    discount, _discounts, _targets = await _calculate_discounts(
        session,
        promos,
        subtotal,
        cart_items,
        user_id,
        email_verified=email_verified,
        check_global_quota=False,
    )

    result = await session.execute(
        text("""
            UPDATE promotions
            SET current_uses = current_uses + 1
            WHERE id = :promo_id
              AND is_active = TRUE
              AND (max_uses IS NULL OR current_uses < max_uses)
            RETURNING id
        """),
        {"promo_id": promo.id},
    )
    if result.fetchone() is None:
        raise _invalid_promo()
    return discount


async def record_promo_usage(
    session: AsyncSession,
    promo_code: str,
    user_id: int,
    order_id: int,
) -> None:
    promo = await session.scalar(select(Promotion).where(Promotion.code == promo_code.upper()))
    if promo is None:
        return
    session.add(PromoCodeUsage(promo_code_id=promo.id, user_id=user_id, order_id=order_id))
    await session.flush()


async def validate_promo(session: AsyncSession, code: str, order_total: float) -> tuple[Promotion, float]:
    """Compatibilite tests anciens : preview single-code sans consommation."""
    promos = await _load_promos(session, [code])
    result = await preview_promos(
        session,
        [code],
        order_total,
        [],
        user_id=0,
        email_verified=True,
    )
    return promos[0], result.discount


def _new_campaign_code(prefix: str, length: int = 6) -> str:
    suffix = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return f"{prefix}-{suffix}"


async def bulk_generate(
    session: AsyncSession,
    body: PromotionCampaignCreate,
    created_by_user_id: int | None,
) -> PromotionCampaignOut:
    campaign = PromotionCampaign(
        name=body.name,
        prefix=body.prefix,
        description=body.description,
        created_by_user_id=created_by_user_id,
    )
    session.add(campaign)
    await session.flush()

    template = body.promotion.model_dump(exclude={"code", "campaign_id", "target_category_ids", "target_product_ids"})
    codes: list[str] = []
    for _ in range(body.count):
        for _attempt in range(20):
            code = _new_campaign_code(body.prefix)
            exists = await session.scalar(select(Promotion.id).where(Promotion.code == code))
            if exists is None and code not in codes:
                break
        else:
            raise AppError("PROMO_CODE_GENERATION_FAILED", "Unable to generate a unique promo code", 409)

        promo = Promotion(**template, code=code, campaign_id=campaign.id)
        session.add(promo)
        await session.flush()
        await _set_targets(
            session,
            promo.id,
            body.promotion.target_category_ids,
            body.promotion.target_product_ids,
        )
        codes.append(code)

    await session.commit()
    await session.refresh(campaign)
    return PromotionCampaignOut.model_validate(campaign).model_copy(update={"codes": codes})


async def list_usages(
    session: AsyncSession,
    promo_id: int,
    pagination: PaginationParams,
) -> tuple[list[PromotionUsageOut], int]:
    if await session.get(Promotion, promo_id) is None:
        raise AppError("PROMOTION_NOT_FOUND", "Promotion not found", 404)

    total = await session.scalar(
        select(func.count(PromoCodeUsage.id)).where(PromoCodeUsage.promo_code_id == promo_id)
    ) or 0
    result = await session.execute(
        select(PromoCodeUsage, Order)
        .join(Order, Order.id == PromoCodeUsage.order_id, isouter=True)
        .where(PromoCodeUsage.promo_code_id == promo_id)
        .order_by(PromoCodeUsage.used_at.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    items: list[PromotionUsageOut] = []
    for usage, order in result.all():
        items.append(
            PromotionUsageOut(
                id=usage.id,
                promo_code_id=usage.promo_code_id,
                user_id=usage.user_id,
                order_id=usage.order_id,
                used_at=usage.used_at,
                order_status=getattr(order, "status", None),
                subtotal=_money(getattr(order, "subtotal", 0)),
                discount_total=_money(getattr(order, "discount_total", 0)),
                total=_money(getattr(order, "total", 0)),
            )
        )
    return items, int(total)


async def cleanup_expired_promotions(session: AsyncSession) -> PromotionCleanupOut:
    result = await session.execute(
        select(Promotion).where(
            Promotion.is_active.is_(True),
            Promotion.ends_at.is_not(None),
            Promotion.ends_at < _now(),
        )
    )
    promos = list(result.scalars().all())
    for promo in promos:
        promo.is_active = False
    await session.commit()
    return PromotionCleanupOut(expired_count=len(promos), promotion_ids=[promo.id for promo in promos])


async def super_admin_stats_for_tenant(session: AsyncSession, tenant_slug: str) -> PromotionSuperAdminStatsOut:
    row = await session.execute(
        select(
            func.count(PromoCodeUsage.id),
            func.count(func.distinct(PromoCodeUsage.user_id)),
            func.coalesce(func.sum(Order.subtotal), 0),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.discount_total), 0),
        )
        .select_from(PromoCodeUsage)
        .join(Order, Order.id == PromoCodeUsage.order_id, isouter=True)
    )
    usage_count, unique_users, gross, net, discount = row.one()
    return PromotionSuperAdminStatsOut(
        tenant_slug=tenant_slug,
        usage_count=int(usage_count or 0),
        unique_users=int(unique_users or 0),
        revenue_gross=_money(gross),
        revenue_net=_money(net),
        discount_total=_money(discount),
    )
