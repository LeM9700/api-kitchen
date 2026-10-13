import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.http.errors import AppError
from app.modules.catalog.models import (
    Extra,
    ExtraIngredient,
    Menu,
    MenuGroup,
    MenuGroupItem,
    Product,
    ProductAvailabilityOverride,
    ProductExtra,
    ProductVariant,
)
from app.modules.delivery.dispatch_service import _items_counts
from app.modules.hr.models import Establishment
from app.modules.orders import service
from app.modules.orders.models import OrderItem
from app.modules.orders.schemas import (
    MenuItemCreate,
    MenuProductSelectionCreate,
    OrderCreate,
    OrderItemExtraCreate,
)
from app.modules.promotions.models import Promotion, PromotionTargetProduct
from app.modules.stock import service as stock_service
from app.modules.stock.models import Ingredient, IngredientBatch, ProductIngredient, VariantIngredient


async def _menu_graph(db_session, *, second_required_group: bool = False):
    suffix = uuid.uuid4().hex[:8]
    establishment = Establishment(
        name=f"Restaurant menus {suffix}",
        timezone="Europe/Paris",
        is_active=True,
    )
    product = Product(
        name=f"Pizza menu {suffix}",
        base_price=12,
        is_active=True,
        preparation_station="kitchen",
    )
    other_product = Product(
        name=f"Boisson menu {suffix}",
        base_price=4,
        is_active=True,
        preparation_station="counter",
    )
    outside_product = Product(name=f"Produit hors menu {suffix}", base_price=3, is_active=True)
    menu = Menu(name=f"Menu midi {suffix}", fixed_price=18, is_active=True)
    db_session.add_all([establishment, product, other_product, outside_product, menu])
    await db_session.flush()

    group = MenuGroup(menu_id=menu.id, name="Plat", min_choices=1, max_choices=1)
    db_session.add(group)
    await db_session.flush()
    db_session.add(MenuGroupItem(group_id=group.id, product_id=product.id))

    second_group = None
    if second_required_group:
        second_group = MenuGroup(menu_id=menu.id, name="Boisson", min_choices=1, max_choices=1)
        db_session.add(second_group)
        await db_session.flush()
        db_session.add(MenuGroupItem(group_id=second_group.id, product_id=other_product.id))

    variant = ProductVariant(product_id=product.id, name="Grande", price_delta=2, is_active=True)
    other_variant = ProductVariant(
        product_id=other_product.id,
        name="50 cl",
        price_delta=1,
        is_active=True,
    )
    extra = Extra(name=f"Mozzarella {suffix}", price=1.5, is_active=True)
    other_extra = Extra(name=f"Citron {suffix}", price=0.5, is_active=True)
    db_session.add_all([variant, other_variant, extra, other_extra])
    await db_session.flush()
    db_session.add(ProductExtra(product_id=product.id, extra_id=extra.id))
    await db_session.flush()
    return {
        "establishment": establishment,
        "menu": menu,
        "group": group,
        "second_group": second_group,
        "product": product,
        "other_product": other_product,
        "outside_product": outside_product,
        "variant": variant,
        "other_variant": other_variant,
        "extra": extra,
        "other_extra": other_extra,
    }


def _body(graph, *, quantity: int = 1, promo_code: str | None = None) -> OrderCreate:
    selections = [
        MenuProductSelectionCreate(
            group_id=graph["group"].id,
            product_id=graph["product"].id,
            variant_id=graph["variant"].id,
            extras=[OrderItemExtraCreate(extra_id=graph["extra"].id, quantity=2)],
        )
    ]
    if graph["second_group"] is not None:
        selections.append(
            MenuProductSelectionCreate(
                group_id=graph["second_group"].id,
                product_id=graph["other_product"].id,
            )
        )
    return OrderCreate(
        establishment_id=graph["establishment"].id,
        order_type="pickup",
        promo_code=promo_code,
        menu_items=[
            MenuItemCreate(
                menu_id=graph["menu"].id,
                quantity=quantity,
                selections=selections,
            )
        ],
    )


def test_order_schema_accepts_menu_only_and_rejects_empty_cart():
    body = OrderCreate(
        order_type="pickup",
        menu_items=[
            MenuItemCreate(
                menu_id=1,
                selections=[MenuProductSelectionCreate(group_id=1, product_id=1)],
            )
        ],
    )
    assert body.items == []
    assert body.menu_items[0].menu_id == 1

    with pytest.raises(ValidationError, match="items ou menu_items"):
        OrderCreate(order_type="pickup")


async def test_menu_order_uses_fixed_price_and_persists_parent_component_snapshots(db_session):
    graph = await _menu_graph(db_session)
    order = await service.create_order(
        db_session,
        _body(graph, quantity=2),
        user_id=None,
        tenant_slug="test",
        idempotency_key=f"menu-{uuid.uuid4().hex}",
        commit=False,
    )

    assert float(order.subtotal) == 36
    assert float(order.total) == 36
    rows = list(
        (
            await db_session.execute(
                select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
            )
        ).scalars()
    )
    assert [row.item_type for row in rows] == ["menu", "menu_component"]
    parent, component = rows
    assert parent.product_id is None
    assert parent.menu_id == graph["menu"].id
    assert float(parent.unit_price) == 18
    assert float(parent.total) == 36
    assert parent.menu_snapshot["groups"][0]["selections"][0]["catalog_unit_price"] == 17
    assert component.parent_item_id == parent.id
    assert component.quantity == 2
    assert float(component.unit_price) == 0
    assert float(component.total) == 0
    assert float(component.catalog_unit_price_snapshot) == 17
    assert component.variant_name_snapshot == "Grande"
    assert component.extras_snapshot[0]["quantity"] == 2
    assert await _items_counts(db_session, [order.id]) == {order.id: 2}

    detail = await service.get_order_detail(db_session, order.id, user_id=None, is_staff=True)
    assert len(detail["items"]) == 1
    assert detail["items"][0]["item_type"] == "menu"
    assert detail["items"][0]["components"][0]["menu_group_name"] == "Plat"
    assert {row["station"] for row in detail["station_summary"]} == {"kitchen"}

    receipt = await service.build_receipt(db_session, order.id)
    assert receipt["items"][0]["label"] == graph["menu"].name
    assert receipt["items"][0]["components"][0]["label"].startswith(graph["product"].name)

    reorder = await service.build_reorder_payload(db_session, order.id, user_id=None, is_staff=True)
    assert reorder["items"] == []
    assert reorder["menu_items"][0]["menu_id"] == graph["menu"].id
    assert reorder["menu_items"][0]["selections"][0]["extras"][0]["extra_id"] == graph["extra"].id


async def test_menu_order_rejects_incomplete_or_forged_composition(db_session):
    graph = await _menu_graph(db_session, second_required_group=True)
    valid_body = _body(graph)

    incomplete = valid_body.model_copy(deep=True)
    incomplete.menu_items[0].selections.pop()
    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            incomplete,
            idempotency_key=f"incomplete-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "INVALID_MENU_SELECTION"

    forged_product = valid_body.model_copy(deep=True)
    forged_product.menu_items[0].selections[0].product_id = graph["outside_product"].id
    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            forged_product,
            idempotency_key=f"product-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "INVALID_MENU_SELECTION"

    forged_variant = valid_body.model_copy(deep=True)
    forged_variant.menu_items[0].selections[0].variant_id = graph["other_variant"].id
    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            forged_variant,
            idempotency_key=f"variant-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "PRODUCT_NOT_FOUND"

    forged_extra = valid_body.model_copy(deep=True)
    forged_extra.menu_items[0].selections[0].extras = [
        OrderItemExtraCreate(extra_id=graph["other_extra"].id)
    ]
    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            forged_extra,
            idempotency_key=f"extra-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "EXTRA_NOT_FOUND"


async def test_menu_order_rejects_inactive_window_and_unavailable_product(db_session):
    graph = await _menu_graph(db_session)
    graph["menu"].available_from = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.flush()

    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            _body(graph),
            idempotency_key=f"window-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "MENU_NOT_AVAILABLE"

    graph["menu"].available_from = None
    db_session.add(
        ProductAvailabilityOverride(
            product_id=graph["product"].id,
            available=False,
            reason="Rupture test",
            changed_by_user_id=1,
        )
    )
    await db_session.flush()
    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            _body(graph),
            idempotency_key=f"availability-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "MENU_PRODUCT_UNAVAILABLE"


async def test_menu_components_drive_stock_and_kds_without_double_counting(db_session, monkeypatch):
    graph = await _menu_graph(db_session)
    order = await service.create_order(
        db_session,
        _body(graph, quantity=2),
        idempotency_key=f"stock-{uuid.uuid4().hex}",
        commit=False,
    )
    ingredient = Ingredient(
        name=f"Ingredient menu {uuid.uuid4().hex[:8]}",
        unit="piece",
        current_qty=100,
        alert_threshold=0,
    )
    db_session.add(ingredient)
    await db_session.flush()
    db_session.add_all(
        [
            IngredientBatch(
                ingredient_id=ingredient.id,
                quantity=100,
                received_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=2),
                status="sealed",
            ),
            ProductIngredient(product_id=graph["product"].id, ingredient_id=ingredient.id, quantity=3),
            VariantIngredient(variant_id=graph["variant"].id, ingredient_id=ingredient.id, quantity=2),
            ExtraIngredient(extra_id=graph["extra"].id, ingredient_id=ingredient.id, quantity=1),
        ]
    )
    await db_session.flush()

    await stock_service.deduct_for_order(db_session, order.id, auto_commit=False)
    assert float(ingredient.current_qty) == 86

    order.status = "preparing"
    await db_session.flush()
    monkeypatch.setattr(service, "notify_staff", AsyncMock())
    await service.update_station_preparation(
        db_session,
        order.id,
        "kitchen",
        "preparing",
        actor_user_id=1,
    )
    detail = await service.update_station_preparation(
        db_session,
        order.id,
        "kitchen",
        "ready",
        actor_user_id=1,
    )
    assert detail["status"] == "ready"
    assert detail["items"][0]["item_type"] == "menu"
    assert detail["items"][0]["components"][0]["preparation_status"] == "ready"


async def test_generic_promotion_applies_to_fixed_menu_price_but_product_target_does_not(
    db_session,
    bootstrap_default_tenant,
):
    graph = await _menu_graph(db_session)
    generic = Promotion(
        code=f"MENU{uuid.uuid4().hex[:6].upper()}",
        discount_type="percent",
        discount_value=10,
        min_order_amount=0,
        is_active=True,
    )
    targeted = Promotion(
        code=f"TARGET{uuid.uuid4().hex[:6].upper()}",
        discount_type="percent",
        discount_value=10,
        min_order_amount=0,
        is_active=True,
    )
    db_session.add_all([generic, targeted])
    await db_session.flush()
    db_session.add(PromotionTargetProduct(promotion_id=targeted.id, product_id=graph["product"].id))
    await db_session.flush()

    order = await service.create_order(
        db_session,
        _body(graph, promo_code=generic.code),
        user_id=bootstrap_default_tenant["client_user_id"],
        tenant_slug="test",
        idempotency_key=f"promo-{uuid.uuid4().hex}",
        commit=False,
    )
    assert float(order.subtotal) == 18
    assert float(order.discount_total) == 1.8
    assert float(order.total) == 16.2

    with pytest.raises(AppError) as exc_info:
        await service.create_order(
            db_session,
            _body(graph, promo_code=targeted.code),
            user_id=bootstrap_default_tenant["client_user_id"],
            tenant_slug="test",
            idempotency_key=f"target-{uuid.uuid4().hex}",
            commit=False,
        )
    assert exc_info.value.code == "INVALID_PROMO"
