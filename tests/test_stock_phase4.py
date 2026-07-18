from contextlib import asynccontextmanager

import pytest

from app.core.http.deps import get_current_user
from app.main import app


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return list(self._items)


class FakeSession:
    def __init__(self, order_items, product_recipes, variant_recipes, extra_recipes, ingredients):
        self.order_items = order_items
        self.product_recipes = product_recipes
        self.variant_recipes = variant_recipes
        self.extra_recipes = extra_recipes
        self.ingredients = ingredients
        self.added = []
        self.committed = False

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"].__name__
        if entity == "OrderItem":
            return FakeResult(self.order_items)
        if entity == "ProductIngredient":
            return FakeResult(self.product_recipes)
        if entity == "VariantIngredient":
            return FakeResult(self.variant_recipes)
        if entity == "ExtraIngredient":
            return FakeResult(self.extra_recipes)
        raise AssertionError(entity)

    async def get(self, model, primary_key):
        if model.__name__ == "Ingredient":
            return self.ingredients.get(primary_key)
        raise AssertionError(model.__name__)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.fixture(autouse=True)
def staff_user_override():
    current_user = {
        "id": "21",
        "tenant_id": 1,
        "tenant_slug": "default",
        "role": "staff",
        "email": "staff@example.test",
    }

    async def _current_user() -> dict:
        return current_user

    app.dependency_overrides[get_current_user] = _current_user
    try:
        yield current_user
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_deduct_for_order_applies_variant_and_extra_ingredients():
    from app.modules.catalog.models import ExtraIngredient
    from app.modules.orders.models import OrderItem
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient, VariantIngredient

    ingredient_base = Ingredient(id=1, name="Pate", unit="kg", current_qty=10, alert_threshold=1)
    ingredient_variant = Ingredient(id=2, name="Truffe", unit="g", current_qty=5, alert_threshold=1)
    ingredient_extra = Ingredient(id=3, name="Burrata", unit="kg", current_qty=8, alert_threshold=1)
    item = OrderItem(
        order_id=10,
        product_id=100,
        variant_id=200,
        quantity=2,
        extras_snapshot=[{"extra_id": 300, "quantity": 1}],
        unit_price=10,
        total=20,
    )
    session = FakeSession(
        order_items=[item],
        product_recipes=[ProductIngredient(product_id=100, ingredient_id=1, quantity=1)],
        variant_recipes=[VariantIngredient(variant_id=200, ingredient_id=2, quantity=0.25)],
        extra_recipes=[ExtraIngredient(extra_id=300, ingredient_id=3, quantity=0.75)],
        ingredients={1: ingredient_base, 2: ingredient_variant, 3: ingredient_extra},
    )

    await service.deduct_for_order(session, 10, auto_commit=False)

    assert float(ingredient_base.current_qty) == 8.0
    assert float(ingredient_variant.current_qty) == 4.5
    assert float(ingredient_extra.current_qty) == 6.5
    deltas = [float(movement.quantity_delta) for movement in session.added]
    assert deltas == [-2.0, -0.5, -1.5]
    assert all(movement.reason == "order:10" for movement in session.added)


@pytest.mark.asyncio
async def test_restore_for_order_applies_variant_and_extra_ingredients():
    from app.modules.catalog.models import ExtraIngredient
    from app.modules.orders.models import OrderItem
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient, VariantIngredient

    ingredient_base = Ingredient(id=1, name="Pate", unit="kg", current_qty=8, alert_threshold=1)
    ingredient_variant = Ingredient(id=2, name="Truffe", unit="g", current_qty=4.5, alert_threshold=1)
    ingredient_extra = Ingredient(id=3, name="Burrata", unit="kg", current_qty=6.5, alert_threshold=1)
    item = OrderItem(
        order_id=10,
        product_id=100,
        variant_id=200,
        quantity=2,
        extras_snapshot=[{"extra_id": 300, "quantity": 1}],
        unit_price=10,
        total=20,
    )
    session = FakeSession(
        order_items=[item],
        product_recipes=[ProductIngredient(product_id=100, ingredient_id=1, quantity=1)],
        variant_recipes=[VariantIngredient(variant_id=200, ingredient_id=2, quantity=0.25)],
        extra_recipes=[ExtraIngredient(extra_id=300, ingredient_id=3, quantity=0.75)],
        ingredients={1: ingredient_base, 2: ingredient_variant, 3: ingredient_extra},
    )

    await service.restore_for_order(session, "default", 10)

    assert float(ingredient_base.current_qty) == 10.0
    assert float(ingredient_variant.current_qty) == 5.0
    assert float(ingredient_extra.current_qty) == 8.0
    deltas = [float(movement.quantity_delta) for movement in session.added]
    assert deltas == [2.0, 0.5, 1.5]
    assert all(movement.reason == "cancel:10" for movement in session.added)


@pytest.mark.asyncio
async def test_deduct_and_restore_for_order_set_actor_user_id():
    """P2-15 : les StockMovement automatiques (deduction/restauration) doivent
    tracer l'utilisateur (staff) qui a declenche la transition de statut, au
    meme titre que les mouvements manuels (supply)."""
    from app.modules.orders.models import OrderItem
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient

    ingredient = Ingredient(id=1, name="Pate", unit="kg", current_qty=10, alert_threshold=1)
    item = OrderItem(order_id=10, product_id=100, quantity=2, unit_price=10, total=20)
    session = FakeSession(
        order_items=[item],
        product_recipes=[ProductIngredient(product_id=100, ingredient_id=1, quantity=1)],
        variant_recipes=[],
        extra_recipes=[],
        ingredients={1: ingredient},
    )

    await service.deduct_for_order(session, 10, auto_commit=False, actor_user_id=21)
    assert all(movement.user_id == 21 for movement in session.added)

    session.added.clear()
    ingredient.current_qty = 8
    await service.restore_for_order(session, "default", 10, actor_user_id=21)
    assert all(movement.user_id == 21 for movement in session.added)


async def test_create_variant_recipe_endpoint_persists_link(client, monkeypatch, staff_user_override):
    from app.modules.stock.models import VariantIngredient

    staff_user_override["role"] = "admin"
    captured = {}

    class RouterSession:
        def add(self, obj):
            captured["obj"] = obj
            obj.id = 501

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug: str):
        yield RouterSession()

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)

    response = await client.post(
        "/api/v1/stock/recipes/variant",
        json={"variant_id": 11, "ingredient_id": 22, "quantity": 0.3},
    )

    assert response.status_code == 201
    assert response.json() == {"id": 501}
    assert isinstance(captured["obj"], VariantIngredient)
    assert captured["obj"].variant_id == 11
    assert captured["obj"].ingredient_id == 22


async def test_create_extra_recipe_endpoint_persists_link(client, monkeypatch, staff_user_override):
    from app.modules.catalog.models import ExtraIngredient

    staff_user_override["role"] = "admin"
    captured = {}

    class RouterSession:
        def add(self, obj):
            captured["obj"] = obj
            obj.id = 601

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug: str):
        yield RouterSession()

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)

    response = await client.post(
        "/api/v1/stock/recipes/extra",
        json={"extra_id": 44, "ingredient_id": 22, "quantity": 0.2},
    )

    assert response.status_code == 201
    assert response.json() == {"id": 601}
    assert isinstance(captured["obj"], ExtraIngredient)
    assert captured["obj"].extra_id == 44
    assert captured["obj"].ingredient_id == 22