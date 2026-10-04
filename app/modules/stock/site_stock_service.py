"""Transactional establishment stock and lot allocation for HACCP phase 3.

This module is deliberately separate from the legacy tenant-wide stock service.
Phase 4 will call these decisions from order/payment entry points. Unknown
remainders are never treated as usable and expired lots cannot be restored.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.catalog.models import (
    Extra,
    ExtraIngredient,
    ProductExtra,
    ProductVariant,
)
from app.modules.orders.models import Order, OrderItem
from app.modules.stock.models import Ingredient, ProductIngredient, VariantIngredient
from app.modules.stock.service import _effective_batch_expires_at, _item_recipe_deltas
from app.modules.stock.site_models import (
    EstablishmentStockMovement,
    StockBatchAllocation,
    StockBatchPosition,
)
from app.modules.stock.models import IngredientBatch


USABLE_STATUSES = ("sealed", "opened")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _usable(position, batch, now: datetime) -> bool:
    if position.remaining_quantity is None or position.remaining_quantity <= 0:
        return False
    if position.excluded_at is not None or batch.status not in USABLE_STATUSES:
        return False
    expires = _effective_batch_expires_at(batch)
    return expires is not None and expires > now


async def _positions(
    session, site_id: int, ingredient_id: int, *, lock: bool, now: datetime
):
    statement = (
        select(StockBatchPosition, IngredientBatch)
        .join(IngredientBatch, IngredientBatch.id == StockBatchPosition.batch_id)
        .where(
            StockBatchPosition.establishment_id == site_id,
            StockBatchPosition.ingredient_id == ingredient_id,
        )
        .order_by(IngredientBatch.id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).all()
    usable = [
        (position, batch) for position, batch in rows if _usable(position, batch, now)
    ]
    # The effective deadline is the earliest primary, secondary, or tertiary
    # DLC; SQL ordering by the primary column alone is not sufficient.
    return sorted(
        usable,
        key=lambda item: (_effective_batch_expires_at(item[1]), item[1].id),
    )


async def usable_quantity(
    session: AsyncSession,
    site_id: int,
    ingredient_id: int,
    *,
    now: datetime | None = None,
) -> Decimal:
    checked_at = now or now_utc()
    return sum(
        (
            Decimal(position.remaining_quantity)
            for position, batch in await _positions(
                session, site_id, ingredient_id, lock=False, now=checked_at
            )
        ),
        Decimal(0),
    )


async def _recipe_requirements(session: AsyncSession, order_id: int):
    items = (
        await session.scalars(
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id)
        )
    ).all()
    requirements: dict[int, Decimal] = {}
    for item in items:
        for ingredient_id, quantity in await _item_recipe_deltas(session, item):
            requirements[ingredient_id] = requirements.get(
                ingredient_id, Decimal(0)
            ) + Decimal(str(quantity))
    return requirements


async def site_product_availability(
    session: AsyncSession,
    site_id: int,
    product_ids: list[int],
    *,
    now: datetime | None = None,
):
    """Return a fail-closed availability map scoped to one establishment."""
    checked_at = now or now_utc()
    recipes = (
        (
            await session.execute(
                select(ProductIngredient).where(
                    ProductIngredient.product_id.in_(product_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    by_product: dict[int, dict[int, Decimal]] = {}
    for recipe in recipes:
        by_product.setdefault(recipe.product_id, {})[recipe.ingredient_id] = (
            by_product.setdefault(recipe.product_id, {}).get(
                recipe.ingredient_id, Decimal(0)
            )
            + Decimal(str(recipe.quantity))
        )
    variants = (
        await session.execute(
            select(ProductVariant.product_id, VariantIngredient)
            .join(VariantIngredient, VariantIngredient.variant_id == ProductVariant.id)
            .where(ProductVariant.product_id.in_(product_ids), ProductVariant.is_active)
        )
    ).all()
    for product_id, recipe in variants:
        by_product.setdefault(product_id, {})[recipe.ingredient_id] = (
            by_product.setdefault(product_id, {}).get(recipe.ingredient_id, Decimal(0))
            + Decimal(str(recipe.quantity))
        )
    extras = (
        await session.execute(
            select(ProductExtra.product_id, ExtraIngredient)
            .join(ExtraIngredient, ExtraIngredient.extra_id == ProductExtra.extra_id)
            .join(Extra, Extra.id == ProductExtra.extra_id)
            .where(ProductExtra.product_id.in_(product_ids), Extra.is_active)
        )
    ).all()
    for product_id, recipe in extras:
        by_product.setdefault(product_id, {})[recipe.ingredient_id] = (
            by_product.setdefault(product_id, {}).get(recipe.ingredient_id, Decimal(0))
            + Decimal(str(recipe.quantity))
        )
    result = {}
    for product_id in product_ids:
        limiting = None
        reason = None
        for ingredient_id, required in by_product.get(product_id, {}).items():
            ingredient = await session.get(Ingredient, ingredient_id)
            available = (
                await usable_quantity(session, site_id, ingredient_id, now=checked_at)
                if ingredient
                else Decimal(0)
            )
            if ingredient is None:
                limiting, reason = str(ingredient_id), "ingredient_missing"
                break
            if available < required:
                limiting, reason = ingredient.name, "expired_or_insufficient_site_stock"
                break
        result[product_id] = {
            "product_id": product_id,
            "available": limiting is None,
            "limiting_ingredient": limiting,
            "reason": reason,
        }
    return result


async def allocate_order(
    session: AsyncSession,
    site_id: int,
    order_id: int,
    *,
    actor_user_id: int | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
):
    """Allocate a complete order FIFO by effective DLC inside the caller transaction."""
    checked_at = now or now_utc()
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.establishment_id == site_id)
        .with_for_update()
    )
    if order is None:
        raise AppError(
            "ORDER_SITE_MISMATCH", "Order does not belong to this establishment", 404
        )
    requirements = await _recipe_requirements(session, order_id)
    existing = (
        await session.scalars(
            select(StockBatchAllocation)
            .where(StockBatchAllocation.order_id == order_id)
            .with_for_update()
        )
    ).all()
    if existing:
        if idempotency_key and await session.scalar(
            select(EstablishmentStockMovement.id).where(
                EstablishmentStockMovement.establishment_id == site_id,
                EstablishmentStockMovement.idempotency_key.like(
                    f"{idempotency_key}:%"
                ),
            )
        ):
            return existing
        raise AppError(
            "STOCK_ORDER_ALREADY_ALLOCATED", "Order stock is already allocated", 409
        )
    chosen: list[tuple[int, int, int, Decimal]] = []
    for ingredient_id, required in requirements.items():
        remaining = required
        positions = await _positions(
            session, site_id, ingredient_id, lock=True, now=checked_at
        )
        for position, batch in positions:
            if remaining <= 0:
                break
            quantity = min(Decimal(position.remaining_quantity), remaining)
            chosen.append(
                (batch.id, ingredient_id, position.establishment_id, quantity)
            )
            remaining -= quantity
        if remaining > 0:
            raise AppError(
                "SITE_STOCK_INSUFFICIENT",
                f"Insufficient usable stock for ingredient {ingredient_id}",
                409,
            )
    allocations = []
    for batch_id, ingredient_id, establishment_id, quantity in chosen:
        position = await session.scalar(
            select(StockBatchPosition)
            .where(
                StockBatchPosition.batch_id == batch_id,
                StockBatchPosition.establishment_id == establishment_id,
                StockBatchPosition.ingredient_id == ingredient_id,
            )
            .with_for_update()
        )
        position.remaining_quantity = Decimal(position.remaining_quantity) - quantity
        allocation = StockBatchAllocation(
            order_id=order_id,
            establishment_id=site_id,
            ingredient_id=ingredient_id,
            batch_id=batch_id,
            quantity=quantity,
            allocated_by=actor_user_id,
        )
        session.add(allocation)
        allocations.append(allocation)
        session.add(
            EstablishmentStockMovement(
                establishment_id=site_id,
                ingredient_id=ingredient_id,
                batch_id=batch_id,
                quantity_delta=-quantity,
                reason=f"order:{order_id}",
                idempotency_key=(
                    f"{idempotency_key}:batch:{batch_id}:{ingredient_id}"
                    if idempotency_key
                    else f"order:{order_id}:batch:{batch_id}:{ingredient_id}"
                ),
                actor_user_id=actor_user_id or 0,
                created_at=checked_at,
            )
        )
    await session.flush()
    return allocations


async def restore_order(
    session: AsyncSession,
    site_id: int,
    order_id: int,
    *,
    actor_user_id: int | None = None,
    now: datetime | None = None,
):
    """Restore only the unreturned quantity of recorded allocations."""
    checked_at = now or now_utc()
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.establishment_id == site_id)
        .with_for_update()
    )
    if order is None:
        raise AppError(
            "ORDER_SITE_MISMATCH", "Order does not belong to this establishment", 404
        )
    allocations = (
        await session.scalars(
            select(StockBatchAllocation)
            .where(
                StockBatchAllocation.order_id == order_id,
                StockBatchAllocation.establishment_id == site_id,
            )
            .with_for_update()
        )
    ).all()
    for allocation in allocations:
        pending = Decimal(allocation.quantity) - Decimal(allocation.restored_quantity)
        if pending <= 0:
            continue
        position = await session.scalar(
            select(StockBatchPosition)
            .where(
                StockBatchPosition.batch_id == allocation.batch_id,
                StockBatchPosition.establishment_id == site_id,
                StockBatchPosition.ingredient_id == allocation.ingredient_id,
            )
            .with_for_update()
        )
        if position is None:
            raise AppError(
                "STOCK_POSITION_MISSING", "Allocated stock position is missing", 409
            )
        batch = await session.get(IngredientBatch, allocation.batch_id)
        allocation.restored_quantity = Decimal(allocation.quantity)
        if batch and _usable(position, batch, checked_at):
            position.remaining_quantity = Decimal(position.remaining_quantity) + pending
        # A returned quantity is always recorded. If the lot expired while the
        # order was active, it is a physical return but remains unavailable;
        # the position is intentionally not increased.
        session.add(
            EstablishmentStockMovement(
                establishment_id=site_id,
                ingredient_id=allocation.ingredient_id,
                batch_id=allocation.batch_id,
                quantity_delta=pending,
                reason=(
                    f"restore:{order_id}"
                    if batch and _usable(position, batch, checked_at)
                    else f"restore_expired:{order_id}"
                ),
                idempotency_key=f"restore:{order_id}:allocation:{allocation.id}",
                actor_user_id=actor_user_id or 0,
                created_at=checked_at,
            )
        )
    await session.flush()
    return allocations
