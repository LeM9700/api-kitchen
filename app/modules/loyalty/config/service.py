"""Configurable loyalty service.

This module owns points computation, rewards, expiration, stats and notification
helpers for tenant-scoped loyalty programs.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytz
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.loyalty.config.models import LoyaltyConfig, LoyaltyReward, LoyaltyRule
from app.modules.loyalty.config.schemas import (
    LoyaltyPointsPreview,
    LoyaltyRewardEligibilityResponse,
    LoyaltyStatsResponse,
    LoyaltyStatsTopReward,
    RedeemResponse,
)
from app.modules.loyalty.account.models import LoyaltyAccount, LoyaltyTransaction
from app.modules.loyalty.account.schemas import ExpiringPointsBucket, ExpiringPointsResponse
from app.modules.loyalty.account.service import get_or_create_account, redeem_points

_PARIS_TZ = pytz.timezone("Europe/Paris")


async def get_or_create_loyalty_config(session: AsyncSession) -> LoyaltyConfig:
    config = await session.scalar(select(LoyaltyConfig))
    if config is None:
        config = LoyaltyConfig(
            base_ratio=1.0,
            points_to_euro_rate=0.01,
            max_cumulative_multiplier=20.0,
            is_active=True,
        )
        session.add(config)
        await session.flush()
    return config


async def compute_points_for_order(
    session: AsyncSession,
    order_total_euros: float,
    category_ids: list[int],
    user_id: int,
) -> LoyaltyPointsPreview:
    config = await get_or_create_loyalty_config(session)
    max_multiplier = float(config.max_cumulative_multiplier)

    if not config.is_active:
        return LoyaltyPointsPreview(
            base_points=0,
            bonus_points=0,
            total_points=0,
            applied_rules=[],
            total_multiplier=Decimal("0"),
            max_multiplier=Decimal(str(max_multiplier)),
            multiplier_was_capped=False,
        )

    base_points = int(float(order_total_euros) * float(config.base_ratio))
    rules_result = await session.execute(
        select(LoyaltyRule)
        .where(LoyaltyRule.is_active.is_(True))
        .order_by(LoyaltyRule.priority.asc())
    )
    rules = list(rules_result.scalars())

    now_paris = datetime.now(_PARIS_TZ)
    today: date = now_paris.date()
    weekday: int = now_paris.weekday()
    category_ids_set = set(category_ids)
    has_previous_order: bool | None = None
    applicable_rules: list[LoyaltyRule] = []

    for rule in rules:
        if rule.rule_type == "first_order":
            if has_previous_order is None:
                has_previous_order = await _has_delivered_order(session, user_id)
            if not has_previous_order:
                applicable_rules.append(rule)
        elif rule.rule_type == "category_multiplier":
            if rule.category_id is not None and rule.category_id in category_ids_set:
                applicable_rules.append(rule)
        elif rule.rule_type == "day_multiplier":
            if rule.days_of_week and weekday in rule.days_of_week:
                applicable_rules.append(rule)
        elif rule.rule_type == "period_multiplier":
            start = rule.start_date
            end = rule.end_date
            if (start is None or today >= start) and (end is None or today <= end):
                applicable_rules.append(rule)

    bonus_multiplier = sum(float(r.multiplier) - 1.0 for r in applicable_rules)
    raw_multiplier = 1.0 + bonus_multiplier
    total_multiplier = min(raw_multiplier, max_multiplier)
    total_points = int(base_points * total_multiplier)

    return LoyaltyPointsPreview(
        base_points=base_points,
        bonus_points=total_points - base_points,
        total_points=total_points,
        applied_rules=[r.name for r in applicable_rules],
        total_multiplier=Decimal(str(total_multiplier)),
        max_multiplier=Decimal(str(max_multiplier)),
        multiplier_was_capped=raw_multiplier > max_multiplier,
    )


async def _has_delivered_order(session: AsyncSession, user_id: int) -> bool:
    from app.modules.orders.models import Order

    count = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.user_id == user_id, Order.status == "delivered")
    )
    return (count or 0) > 0


async def credit_points_for_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    order_total_euros: float,
    category_ids: list[int],
) -> LoyaltyAccount:
    idempotency_key = f"order_delivered_{order_id}"
    already_credited = await session.scalar(
        select(LoyaltyTransaction).where(LoyaltyTransaction.reason == idempotency_key).limit(1)
    )
    if already_credited is not None:
        return await get_or_create_account(session, user_id)

    preview = await compute_points_for_order(session, order_total_euros, category_ids, user_id)
    account = await get_or_create_account(session, user_id, commit=False)
    if preview.total_points <= 0:
        await session.commit()
        return account

    account.points += preview.total_points
    session.add(
        LoyaltyTransaction(
            account_id=account.id,
            points_delta=preview.total_points,
            reason=idempotency_key,
            transaction_type="earn",
            source="order",
            order_id=order_id,
            metadata_json={
                "base_points": preview.base_points,
                "bonus_points": preview.bonus_points,
                "applied_rules": preview.applied_rules,
                "total_multiplier": str(preview.total_multiplier),
            },
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


async def check_and_expire_points(session: AsyncSession, user_id: int) -> int:
    config = await get_or_create_loyalty_config(session)
    if config.points_expiry_days is None:
        return 0

    account = await get_or_create_account(session, user_id, commit=False)
    if account.points <= 0:
        await session.commit()
        return 0

    cutoff = datetime.now(_PARIS_TZ).replace(tzinfo=None) - timedelta(days=config.points_expiry_days)
    expired_points_result = await session.scalar(
        select(func.sum(LoyaltyTransaction.points_delta)).where(
            LoyaltyTransaction.account_id == account.id,
            LoyaltyTransaction.points_delta > 0,
            LoyaltyTransaction.transaction_type == "earn",
            LoyaltyTransaction.created_at < cutoff,
        )
    )
    expired_points = int(expired_points_result or 0)
    if expired_points <= 0:
        await session.commit()
        return 0

    to_deduct = min(expired_points, account.points)
    account.points -= to_deduct
    session.add(
        LoyaltyTransaction(
            account_id=account.id,
            points_delta=-to_deduct,
            reason="expired",
            transaction_type="expire",
            source="system",
        )
    )
    await session.commit()
    return to_deduct


async def check_and_expire_points_for_all_users(session: AsyncSession) -> int:
    config = await get_or_create_loyalty_config(session)
    if config.points_expiry_days is None:
        return 0

    result = await session.execute(select(LoyaltyAccount.user_id))
    user_ids = [row[0] for row in result]
    total_expired = 0
    for uid in user_ids:
        total_expired += await check_and_expire_points(session, uid)
    return total_expired


async def get_expiring_points(session: AsyncSession, user_id: int) -> ExpiringPointsResponse:
    config = await get_or_create_loyalty_config(session)
    if config.points_expiry_days is None:
        return ExpiringPointsResponse(points_expiry_days=None, total_expiring_points=0, buckets=[])

    account = await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id))
    if account is None:
        return ExpiringPointsResponse(points_expiry_days=config.points_expiry_days, total_expiring_points=0, buckets=[])

    now = datetime.now(_PARIS_TZ).replace(tzinfo=None)
    buckets: list[ExpiringPointsBucket] = []
    for days in (30, 7, 1):
        cutoff = now - timedelta(days=config.points_expiry_days - days)
        points = await session.scalar(
            select(func.coalesce(func.sum(LoyaltyTransaction.points_delta), 0)).where(
                LoyaltyTransaction.account_id == account.id,
                LoyaltyTransaction.points_delta > 0,
                LoyaltyTransaction.transaction_type == "earn",
                LoyaltyTransaction.created_at <= cutoff,
            )
        )
        bucket_points = min(int(points or 0), account.points)
        if bucket_points > 0:
            buckets.append(ExpiringPointsBucket(days_until_expiry=days, points=bucket_points))

    total = max((bucket.points for bucket in buckets), default=0)
    return ExpiringPointsResponse(
        points_expiry_days=config.points_expiry_days,
        total_expiring_points=total,
        buckets=buckets,
    )


async def notify_expiring_points_for_all_users(
    session: AsyncSession,
    tenant_slug: str,
    redis=None,
) -> int:
    from app.modules.notifications.notification_service import notify_user

    result = await session.execute(select(LoyaltyAccount))
    accounts = list(result.scalars())
    sent = 0
    today = datetime.now(timezone.utc).date().isoformat()
    for account in accounts:
        expiring = await get_expiring_points(session, account.user_id)
        for bucket in expiring.buckets:
            reason = f"expiry_notice_{bucket.days_until_expiry}_{today}"
            already_sent = await session.scalar(
                select(LoyaltyTransaction.id).where(
                    LoyaltyTransaction.account_id == account.id,
                    LoyaltyTransaction.reason == reason,
                )
            )
            if already_sent:
                continue
            await notify_user(
                session=session,
                tenant_slug=tenant_slug,
                user_id=account.user_id,
                event="loyalty.points_expiring",
                title="Points de fidelite bientot expires",
                body=f"{bucket.points} points expirent dans {bucket.days_until_expiry} jour(s).",
                data={"points": bucket.points, "days_until_expiry": bucket.days_until_expiry},
                redis=redis,
            )
            session.add(
                LoyaltyTransaction(
                    account_id=account.id,
                    points_delta=0,
                    reason=reason,
                    transaction_type="adjustment",
                    source="system",
                    metadata_json={"notice": "expiry", "days_until_expiry": bucket.days_until_expiry},
                )
            )
            await session.commit()
            sent += 1
    return sent


async def list_available_rewards(session: AsyncSession, user_points: int) -> list[LoyaltyReward]:
    result = await session.execute(
        select(LoyaltyReward)
        .where(LoyaltyReward.is_active.is_(True), LoyaltyReward.points_required <= user_points)
        .order_by(LoyaltyReward.points_required.asc())
    )
    return list(result.scalars())


async def list_reward_catalog_with_eligibility(
    session: AsyncSession,
    user_points: int,
) -> list[LoyaltyRewardEligibilityResponse]:
    result = await session.execute(
        select(LoyaltyReward)
        .where(LoyaltyReward.is_active.is_(True))
        .order_by(LoyaltyReward.points_required.asc())
    )
    return [
        LoyaltyRewardEligibilityResponse(
            id=reward.id,
            name=reward.name,
            reward_type=reward.reward_type,
            points_required=reward.points_required,
            discount_amount=reward.discount_amount,
            product_id=reward.product_id,
            is_active=reward.is_active,
            created_at=reward.created_at,
            can_redeem=user_points >= reward.points_required,
            missing_points=max(0, reward.points_required - user_points),
        )
        for reward in result.scalars()
    ]


async def redeem_reward(
    session: AsyncSession,
    user_id: int,
    reward_id: int,
) -> RedeemResponse:
    """Échange des points fidélité contre une récompense.

    Pour les récompenses de type ``discount_euros``, génère automatiquement un code
    promo à usage unique lié à l'utilisateur. Ce code est utilisable dans le champ
    ``promo_code`` de ``POST /orders``.

    Pour les récompenses de type ``free_product``, retourne le ``free_product_id``
    que le frontend peut ajouter directement au panier.

    Args:
        session: Session SQLAlchemy du tenant.
        user_id: Identifiant de l'utilisateur qui rachète la récompense.
        reward_id: Identifiant de la récompense à racheter.

    Returns:
        RedeemResponse contenant le code promo (si discount) ou le produit offert.

    Raises:
        AppError: REWARD_NOT_FOUND si la récompense est inactive ou inexistante.
        AppError: INSUFFICIENT_POINTS si le solde est insuffisant.
    """
    import uuid
    from app.modules.promotions.models import Promotion

    reward = await session.get(LoyaltyReward, reward_id)
    if reward is None or not reward.is_active:
        raise AppError("REWARD_NOT_FOUND", "Recompense introuvable ou inactive", 404)

    account = await redeem_points(
        session,
        user_id,
        reward.points_required,
        reason=f"redeem_reward_{reward_id}",
        source="reward",
        reward_id=reward_id,
    )

    promo_code: str | None = None

    if reward.reward_type == "discount_euros" and reward.discount_amount is not None:
        # Générer un code promo à usage unique lié à cet utilisateur.
        # Le code est non-public (invisible dans la liste publique) et ne peut être
        # utilisé qu'une seule fois, uniquement par cet utilisateur.
        code = f"REWARD-{uuid.uuid4().hex[:10].upper()}"
        promo = Promotion(
            code=code,
            description=f"Récompense fidélité : {reward.name}",
            discount_type="fixed",
            discount_value=float(reward.discount_amount),
            max_uses=1,
            max_uses_per_user=1,
            user_id=user_id,
            is_public=False,
            is_active=True,
        )
        session.add(promo)
        await session.commit()
        await session.refresh(promo)
        promo_code = promo.code

    return RedeemResponse(
        discount_euros=float(reward.discount_amount) if reward.reward_type == "discount_euros" else None,
        free_product_id=reward.product_id if reward.reward_type == "free_product" else None,
        remaining_points=account.points,
        promo_code=promo_code,
    )


def validate_rule_state(rule: LoyaltyRule) -> None:
    if rule.rule_type == "category_multiplier" and rule.category_id is None:
        raise AppError("INVALID_RULE", "category_id is required for category_multiplier", 422, "category_id")
    if rule.rule_type == "day_multiplier" and not rule.days_of_week:
        raise AppError("INVALID_RULE", "days_of_week is required for day_multiplier", 422, "days_of_week")
    if rule.rule_type == "period_multiplier":
        if rule.start_date is None or rule.end_date is None:
            raise AppError("INVALID_RULE", "start_date and end_date are required for period_multiplier", 422)
        if rule.end_date < rule.start_date:
            raise AppError("INVALID_RULE", "end_date must be after start_date", 422, "end_date")


def validate_reward_state(reward: LoyaltyReward) -> None:
    if reward.reward_type == "discount_euros" and reward.discount_amount is None:
        raise AppError("INVALID_REWARD", "discount_amount is required for discount_euros", 422, "discount_amount")
    if reward.reward_type == "free_product" and reward.product_id is None:
        raise AppError("INVALID_REWARD", "product_id is required for free_product", 422, "product_id")


async def get_loyalty_stats(
    session: AsyncSession,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> LoyaltyStatsResponse:
    tx_filters = []
    if date_from is not None:
        tx_filters.append(LoyaltyTransaction.created_at >= date_from)
    if date_to is not None:
        tx_filters.append(LoyaltyTransaction.created_at <= date_to)

    member_count = int(await session.scalar(select(func.count()).select_from(LoyaltyAccount)) or 0)
    active_member_count = int(
        await session.scalar(
            select(func.count(func.distinct(LoyaltyTransaction.account_id))).where(*tx_filters)
        )
        or 0
    )
    points_distributed = int(
        await session.scalar(
            select(func.coalesce(func.sum(LoyaltyTransaction.points_delta), 0)).where(
                LoyaltyTransaction.points_delta > 0,
                *tx_filters,
            )
        )
        or 0
    )
    points_redeemed = abs(
        int(
            await session.scalar(
                select(func.coalesce(func.sum(LoyaltyTransaction.points_delta), 0)).where(
                    LoyaltyTransaction.transaction_type == "redeem",
                    *tx_filters,
                )
            )
            or 0
        )
    )
    points_expired = abs(
        int(
            await session.scalar(
                select(func.coalesce(func.sum(LoyaltyTransaction.points_delta), 0)).where(
                    LoyaltyTransaction.transaction_type == "expire",
                    *tx_filters,
                )
            )
            or 0
        )
    )
    circulating_balance = int(
        await session.scalar(select(func.coalesce(func.sum(LoyaltyAccount.points), 0))) or 0
    )

    counts_result = await session.execute(
        select(LoyaltyTransaction.transaction_type, func.count())
        .where(*tx_filters)
        .group_by(LoyaltyTransaction.transaction_type)
    )
    counts = {row[0]: int(row[1]) for row in counts_result}

    top_result = await session.execute(
        select(
            LoyaltyTransaction.reward_id,
            func.count().label("redemptions"),
            func.abs(func.coalesce(func.sum(LoyaltyTransaction.points_delta), 0)).label("points_redeemed"),
        )
        .where(
            LoyaltyTransaction.reward_id.is_not(None),
            LoyaltyTransaction.transaction_type == "redeem",
            *tx_filters,
        )
        .group_by(LoyaltyTransaction.reward_id)
        .order_by(func.count().desc())
        .limit(10)
    )
    top_rewards = [
        LoyaltyStatsTopReward(
            reward_id=int(row.reward_id),
            redemptions=int(row.redemptions),
            points_redeemed=int(row.points_redeemed or 0),
        )
        for row in top_result
    ]
    redemption_rate = (points_redeemed / points_distributed) if points_distributed > 0 else 0.0

    return LoyaltyStatsResponse(
        member_count=member_count,
        active_member_count=active_member_count,
        points_distributed=points_distributed,
        points_redeemed=points_redeemed,
        points_expired=points_expired,
        circulating_balance=circulating_balance,
        redemption_rate=redemption_rate,
        transaction_counts_by_type=counts,
        top_rewards=top_rewards,
    )
