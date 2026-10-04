"""Establishment-scoped stock and lot allocation invariants."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.http.errors import AppError
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from app.modules.stock.models import Ingredient, IngredientBatch, ProductIngredient
from app.modules.stock.site_models import (
    EstablishmentStock,
    EstablishmentStockMovement,
    StockBatchAllocation,
    StockBatchPosition,
)
from app.modules.stock.site_stock_service import (
    allocate_order,
    restore_order,
    site_product_availability,
    usable_quantity,
)
from app.modules.hr.models import Establishment

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


async def add(session, model, **values):
    instance = model(**values)
    session.add(instance)
    await session.flush()
    return instance


@pytest.fixture
async def stock_graph(db_session):
    site_a = await add(db_session, Establishment, name="Stock A", timezone="UTC")
    site_b = await add(db_session, Establishment, name="Stock B", timezone="UTC")
    ingredient = await add(
        db_session, Ingredient, name="Tomate", unit="kg", current_qty=20
    )
    product = await add(db_session, Product, name="Pizza tomate", base_price=10)
    db_session.add(
        ProductIngredient(
            product_id=product.id, ingredient_id=ingredient.id, quantity=Decimal("1.5")
        )
    )
    await db_session.flush()
    stock_a = await add(
        db_session,
        EstablishmentStock,
        establishment_id=site_a.id,
        ingredient_id=ingredient.id,
        physical_quantity=10,
        reconciled_at=NOW,
        reconciled_by=7,
    )
    stock_b = await add(
        db_session,
        EstablishmentStock,
        establishment_id=site_b.id,
        ingredient_id=ingredient.id,
        physical_quantity=10,
        reconciled_at=NOW,
        reconciled_by=7,
    )
    good = await add(
        db_session,
        IngredientBatch,
        ingredient_id=ingredient.id,
        quantity=3,
        received_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    replacement = await add(
        db_session,
        IngredientBatch,
        ingredient_id=ingredient.id,
        quantity=4,
        received_at=NOW,
        expires_at=NOW + timedelta(days=2),
    )
    expired = await add(
        db_session,
        IngredientBatch,
        ingredient_id=ingredient.id,
        quantity=4,
        received_at=NOW - timedelta(days=3),
        expires_at=NOW,
    )
    good_a = StockBatchPosition(
        batch_id=good.id,
        establishment_id=site_a.id,
        ingredient_id=ingredient.id,
        remaining_quantity=3,
    )
    replacement_a = StockBatchPosition(
        batch_id=replacement.id,
        establishment_id=site_a.id,
        ingredient_id=ingredient.id,
        remaining_quantity=4,
    )
    expired_a = StockBatchPosition(
        batch_id=expired.id,
        establishment_id=site_a.id,
        ingredient_id=ingredient.id,
        remaining_quantity=4,
        excluded_at=NOW,
        excluded_by=7,
        exclusion_reason="DLC échue",
    )
    good_b = StockBatchPosition(
        batch_id=good.id,
        establishment_id=site_b.id,
        ingredient_id=ingredient.id,
        remaining_quantity=3,
    )
    db_session.add_all([good_a, replacement_a, expired_a, good_b])
    await db_session.flush()
    return SimpleNamespace(
        site_a=site_a,
        site_b=site_b,
        ingredient=ingredient,
        product=product,
        good=good,
        replacement=replacement,
        expired=expired,
        stock_a=stock_a,
        stock_b=stock_b,
        good_a=good_a,
        replacement_a=replacement_a,
        expired_a=expired_a,
        good_b=good_b,
    )


async def create_order(session, site_id, product_id, quantity=1):
    order = await add(
        session, Order, establishment_id=site_id, order_type="pickup", status="pending"
    )
    await add(
        session,
        OrderItem,
        order_id=order.id,
        product_id=product_id,
        quantity=quantity,
        unit_price=10,
        total=10,
    )
    return order


async def test_availability_is_local_and_uses_only_valid_lots(db_session, stock_graph):
    graph = stock_graph
    available_a = await site_product_availability(
        db_session, graph.site_a.id, [graph.product.id], now=NOW
    )
    available_b = await site_product_availability(
        db_session, graph.site_b.id, [graph.product.id], now=NOW
    )
    assert available_a[graph.product.id]["available"]
    assert available_b[graph.product.id]["available"]
    graph.good_a.remaining_quantity = 0
    graph.replacement_a.remaining_quantity = 0
    await db_session.flush()
    unavailable = await site_product_availability(
        db_session, graph.site_a.id, [graph.product.id], now=NOW
    )
    assert unavailable[graph.product.id]["available"] is False
    assert (
        unavailable[graph.product.id]["reason"] == "expired_or_insufficient_site_stock"
    )


async def test_unknown_remainder_fails_closed(db_session, stock_graph):
    graph = stock_graph
    position = await db_session.scalar(
        select(StockBatchPosition).where(
            StockBatchPosition.batch_id == graph.good.id,
            StockBatchPosition.establishment_id == graph.site_a.id,
        )
    )
    position.remaining_quantity = None
    await db_session.flush()
    assert await usable_quantity(
        db_session, graph.site_a.id, graph.ingredient.id, now=NOW
    ) == Decimal("4")
    order = await create_order(
        db_session, graph.site_a.id, graph.product.id, quantity=4
    )
    with pytest.raises(AppError) as error:
        await allocate_order(db_session, graph.site_a.id, order.id, now=NOW)
    assert error.value.code == "SITE_STOCK_INSUFFICIENT"


async def test_allocation_is_fifo_by_effective_dlc_and_replay_safe(
    db_session, stock_graph
):
    graph = stock_graph
    order = await create_order(
        db_session, graph.site_a.id, graph.product.id, quantity=4
    )
    allocations = await allocate_order(
        db_session,
        graph.site_a.id,
        order.id,
        actor_user_id=7,
        idempotency_key="alloc-1",
        now=NOW,
    )
    assert [(item.batch_id, Decimal(item.quantity)) for item in allocations] == [
        (graph.good.id, Decimal("3")),
        (graph.replacement.id, Decimal("3")),
    ]
    good = await db_session.scalar(
        select(StockBatchPosition).where(
            StockBatchPosition.batch_id == graph.good.id,
            StockBatchPosition.establishment_id == graph.site_a.id,
        )
    )
    replacement = await db_session.scalar(
        select(StockBatchPosition).where(
            StockBatchPosition.batch_id == graph.replacement.id,
            StockBatchPosition.establishment_id == graph.site_a.id,
        )
    )
    assert good.remaining_quantity == 0 and replacement.remaining_quantity == 1
    with pytest.raises(AppError) as error:
        await allocate_order(
            db_session, graph.site_a.id, order.id, idempotency_key="different", now=NOW
        )
    assert error.value.code == "STOCK_ORDER_ALREADY_ALLOCATED"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockBatchAllocation)
            .where(StockBatchAllocation.order_id == order.id)
        )
        == 2
    )


async def test_site_and_tenant_mismatch_cannot_allocate(db_session, stock_graph):
    graph = stock_graph
    order = await create_order(db_session, graph.site_a.id, graph.product.id)
    with pytest.raises(AppError) as error:
        await allocate_order(db_session, graph.site_b.id, order.id, now=NOW)
    assert error.value.code == "ORDER_SITE_MISMATCH"


async def test_restore_is_idempotent_and_expired_lot_stays_unusable(
    db_session, stock_graph
):
    graph = stock_graph
    order = await create_order(db_session, graph.site_a.id, graph.product.id)
    await allocate_order(
        db_session,
        graph.site_a.id,
        order.id,
        actor_user_id=7,
        idempotency_key="alloc-restore",
        now=NOW,
    )
    graph.good.expires_at = NOW
    await db_session.flush()
    await restore_order(
        db_session,
        graph.site_a.id,
        order.id,
        actor_user_id=7,
        now=NOW + timedelta(seconds=1),
    )
    await restore_order(
        db_session,
        graph.site_a.id,
        order.id,
        actor_user_id=7,
        now=NOW + timedelta(seconds=2),
    )
    assert await usable_quantity(
        db_session, graph.site_a.id, graph.ingredient.id, now=NOW + timedelta(seconds=2)
    ) == Decimal("4")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(EstablishmentStockMovement)
            .where(
                EstablishmentStockMovement.establishment_id == graph.site_a.id,
                EstablishmentStockMovement.reason.like("restore%"),
            )
        )
        == 1
    )
    allocation = await db_session.scalar(
        select(StockBatchAllocation).where(StockBatchAllocation.order_id == order.id)
    )
    assert allocation.restored_quantity == allocation.quantity


async def test_expired_replacement_keeps_other_site_untouched(db_session, stock_graph):
    graph = stock_graph
    graph.good_a.excluded_at = NOW
    graph.good_a.excluded_by = 7
    graph.good_a.exclusion_reason = "DLC écartée sur site A"
    await db_session.flush()
    assert await usable_quantity(
        db_session, graph.site_a.id, graph.ingredient.id, now=NOW
    ) == Decimal("4")
    assert await usable_quantity(
        db_session, graph.site_b.id, graph.ingredient.id, now=NOW
    ) == Decimal("3")
