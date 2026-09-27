from contextlib import asynccontextmanager

import pytest

from app.core.http.deps import get_current_user
from app.main import app


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return [item[0] if isinstance(item, tuple) else item for item in self._items]

    def all(self):
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
        entity = statement.column_descriptions[0].get("entity")
        entity_name = entity.__name__ if entity is not None else ""
        column_name = statement.column_descriptions[0].get("name")
        if entity_name == "OrderItem":
            return FakeResult(self.order_items)
        if entity_name == "ProductIngredient":
            if column_name == "product_id":
                return FakeResult([recipe.product_id for recipe in self.product_recipes])
            return FakeResult(self.product_recipes)
        if entity_name == "VariantIngredient":
            return FakeResult(self.variant_recipes)
        if entity_name == "ExtraIngredient":
            return FakeResult(self.extra_recipes)
        if entity_name == "Ingredient":
            return FakeResult(self.ingredients.values())
        if entity_name == "ProductVariant":
            rows = []
            for recipe in self.variant_recipes:
                product_id = next(
                    (
                        item.product_id
                        for item in self.order_items
                        if item.variant_id == recipe.variant_id
                    ),
                    None,
                )
                if product_id is not None:
                    if len(statement.column_descriptions) > 1:
                        rows.append((product_id, recipe))
                    else:
                        rows.append(product_id)
            return FakeResult(rows)
        if entity_name == "ProductExtra":
            rows = []
            for recipe in self.extra_recipes:
                product_id = next(
                    (
                        item.product_id
                        for item in self.order_items
                        for extra in (item.extras_snapshot or [])
                        if extra.get("extra_id") == recipe.extra_id
                    ),
                    None,
                )
                if product_id is not None:
                    if len(statement.column_descriptions) > 1:
                        rows.append((product_id, recipe))
                    else:
                        rows.append(product_id)
            return FakeResult(rows)
        if entity_name == "ProductAvailabilityOverride":
            return FakeResult([])
        raise AssertionError(entity_name)

    async def get(self, model, primary_key):
        if model.__name__ == "Ingredient":
            return self.ingredients.get(primary_key)
        raise AssertionError(model.__name__)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = 1000 + index

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
async def test_deduct_for_order_creates_auto_unavailable_override_when_stock_blocks_next_sale():
    from app.modules.catalog.models import ProductAvailabilityOverride
    from app.modules.orders.models import OrderItem
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient

    ingredient = Ingredient(id=1, name="Pate", unit="piece", current_qty=1.5, alert_threshold=1)
    item = OrderItem(
        order_id=10,
        product_id=100,
        quantity=1,
        unit_price=10,
        total=10,
    )
    session = FakeSession(
        order_items=[item],
        product_recipes=[ProductIngredient(product_id=100, ingredient_id=1, quantity=1)],
        variant_recipes=[],
        extra_recipes=[],
        ingredients={1: ingredient},
    )

    await service.deduct_for_order(session, 10, auto_commit=False)

    overrides = [item for item in session.added if isinstance(item, ProductAvailabilityOverride)]
    assert len(overrides) == 1
    assert overrides[0].product_id == 100
    assert overrides[0].available is False
    assert overrides[0].reason == "Stock insuffisant : Pate"


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
    staff_user_override["role"] = "admin"
    captured = {}

    async def fake_create_recipe_line(session, recipe_type, target_id, item, user_id=None):
        captured["recipe_type"] = recipe_type
        captured["target_id"] = target_id
        captured["ingredient_id"] = item.ingredient_id
        captured["quantity"] = item.quantity
        captured["user_id"] = user_id
        return {"id": 501}

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug: str):
        yield object()

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.router.service.create_recipe_line", fake_create_recipe_line)

    response = await client.post(
        "/api/v1/stock/recipes/variant",
        json={"variant_id": 11, "ingredient_id": 22, "quantity": 0.3},
    )

    assert response.status_code == 201
    assert response.json() == {"id": 501}
    assert captured == {
        "recipe_type": "variant",
        "target_id": 11,
        "ingredient_id": 22,
        "quantity": 0.3,
        "user_id": 21,
    }


async def test_create_extra_recipe_endpoint_persists_link(client, monkeypatch, staff_user_override):
    staff_user_override["role"] = "admin"
    captured = {}

    async def fake_create_recipe_line(session, recipe_type, target_id, item, user_id=None):
        captured["recipe_type"] = recipe_type
        captured["target_id"] = target_id
        captured["ingredient_id"] = item.ingredient_id
        captured["quantity"] = item.quantity
        captured["user_id"] = user_id
        return {"id": 601}

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug: str):
        yield object()

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.router.service.create_recipe_line", fake_create_recipe_line)

    response = await client.post(
        "/api/v1/stock/recipes/extra",
        json={"extra_id": 44, "ingredient_id": 22, "quantity": 0.2},
    )

    assert response.status_code == 201
    assert response.json() == {"id": 601}
    assert captured == {
        "recipe_type": "extra",
        "target_id": 44,
        "ingredient_id": 22,
        "quantity": 0.2,
        "user_id": 21,
    }


async def test_create_recipe_endpoint_rejects_non_positive_quantity(client, staff_user_override):
    staff_user_override["role"] = "admin"

    response = await client.post(
        "/api/v1/stock/recipes",
        json={"product_id": 11, "ingredient_id": 22, "quantity": 0},
    )

    assert response.status_code == 422


class RecipeServiceSession:
    def __init__(self, *, targets=None, ingredients=None, recipes=None):
        self.targets = targets or {}
        self.ingredients = ingredients or {}
        self.recipes = recipes or []
        self.added = []
        self.deleted = []
        self.committed = False
        self.flushed = False

    async def get(self, model, primary_key):
        if model.__name__ == "Ingredient":
            return self.ingredients.get(primary_key)
        return self.targets.get((model.__name__, primary_key))

    async def execute(self, statement):
        if getattr(statement, "is_delete", False):
            self.deleted.append(statement)
            self.recipes.clear()
            return FakeResult([])

        entity = statement.column_descriptions[0].get("entity")
        entity_name = entity.__name__ if entity is not None else ""
        column_name = statement.column_descriptions[0].get("name")
        if entity_name == "Ingredient":
            return FakeResult(self.ingredients.values())
        if entity_name == "ProductIngredient" and column_name == "product_id":
            return FakeResult([recipe.product_id for recipe in self.recipes])
        if entity_name in {"ProductIngredient", "VariantIngredient", "ExtraIngredient"}:
            return FakeResult(self.recipes)
        if entity_name in {"ProductVariant", "ProductExtra", "ProductAvailabilityOverride"}:
            return FakeResult([])
        raise AssertionError(entity_name)

    async def scalar(self, statement):
        result = await self.execute(statement)
        values = list(result.scalars())
        return values[0] if values else None

    def add(self, obj):
        self.added.append(obj)
        if obj.__class__.__name__ in {"ProductIngredient", "VariantIngredient", "ExtraIngredient"}:
            self.recipes.append(obj)

    async def delete(self, obj):
        self.recipes.remove(obj)
        self.deleted.append(obj)

    async def flush(self):
        self.flushed = True
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = 1000 + index

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_replace_product_recipe_validates_target_and_replaces_lines():
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient
    from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={
            1: Ingredient(id=1, name="Pate", unit="piece", current_qty=10, alert_threshold=2),
            2: Ingredient(id=2, name="Mozzarella", unit="kg", current_qty=3, alert_threshold=1),
        },
    )

    response = await service.replace_recipe(
        session,
        "product",
        100,
        StockRecipeReplace(
            items=[
                StockRecipeLineCreate(ingredient_id=1, quantity=1),
                StockRecipeLineCreate(ingredient_id=2, quantity=120, unit="g"),
            ],
        ),
    )

    assert response["recipe_type"] == "product"
    assert response["target_id"] == 100
    assert [item["ingredient_name"] for item in response["items"]] == ["Pate", "Mozzarella"]
    assert [item["unit"] for item in response["items"]] == ["piece", "kg"]
    assert [float(recipe.quantity) for recipe in session.recipes] == [1.0, 0.12]
    assert session.flushed is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_replace_product_recipe_records_audit_log_with_old_and_new_items():
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient
    from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={
            1: Ingredient(id=1, name="Pate", unit="piece", current_qty=10, alert_threshold=2),
            2: Ingredient(id=2, name="Mozzarella", unit="kg", current_qty=3, alert_threshold=1),
        },
        recipes=[ProductIngredient(id=7, product_id=100, ingredient_id=1, quantity=1, quantity_unit="piece")],
    )

    await service.replace_recipe(
        session,
        "product",
        100,
        StockRecipeReplace(
            items=[
                StockRecipeLineCreate(ingredient_id=1, quantity=1.5, unit="piece"),
                StockRecipeLineCreate(ingredient_id=2, quantity=120, unit="g"),
            ],
        ),
        user_id=21,
    )

    audit_logs = [item for item in session.added if item.__class__.__name__ == "StockRecipeAuditLog"]
    assert len(audit_logs) == 1
    audit_log = audit_logs[0]
    assert audit_log.recipe_type == "product"
    assert audit_log.target_id == 100
    assert audit_log.changed_by_user_id == 21
    assert audit_log.old_items == [
        {
            "id": 7,
            "recipe_type": "product",
            "target_id": 100,
            "ingredient_id": 1,
            "ingredient_name": "Pate",
            "quantity": 1.0,
            "unit": "piece",
        }
    ]
    assert [item["ingredient_name"] for item in audit_log.new_items] == ["Pate", "Mozzarella"]
    assert [item["quantity"] for item in audit_log.new_items] == [1.5, 0.12]


@pytest.mark.asyncio
async def test_replace_recipe_rejects_missing_ingredient():
    from app.core.http.errors import AppError
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={},
    )

    with pytest.raises(AppError) as exc_info:
        await service.replace_recipe(
            session,
            "product",
            100,
            StockRecipeReplace(items=[StockRecipeLineCreate(ingredient_id=99, quantity=1)]),
        )

    assert exc_info.value.code == "INGREDIENT_NOT_FOUND"
    assert exc_info.value.status_code == 404
    assert session.committed is False


@pytest.mark.asyncio
async def test_replace_recipe_rejects_duplicate_ingredient():
    from app.core.http.errors import AppError
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient
    from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={1: Ingredient(id=1, name="Pate", unit="piece", current_qty=10, alert_threshold=2)},
    )

    with pytest.raises(AppError) as exc_info:
        await service.replace_recipe(
            session,
            "product",
            100,
            StockRecipeReplace(
                items=[
                    StockRecipeLineCreate(ingredient_id=1, quantity=1),
                    StockRecipeLineCreate(ingredient_id=1, quantity=2),
                ],
            ),
        )

    assert exc_info.value.code == "DUPLICATE_RECIPE_INGREDIENT"
    assert exc_info.value.status_code == 409
    assert session.committed is False


@pytest.mark.asyncio
async def test_replace_recipe_rejects_incompatible_unit():
    from app.core.http.errors import AppError
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient
    from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={1: Ingredient(id=1, name="Mozzarella", unit="kg", current_qty=10, alert_threshold=2)},
    )

    with pytest.raises(AppError) as exc_info:
        await service.replace_recipe(
            session,
            "product",
            100,
            StockRecipeReplace(items=[StockRecipeLineCreate(ingredient_id=1, quantity=1, unit="ml")]),
        )

    assert exc_info.value.code == "INVALID_RECIPE_UNIT"
    assert exc_info.value.status_code == 422
    assert session.committed is False


@pytest.mark.asyncio
async def test_create_recipe_line_rejects_existing_ingredient_link():
    from app.core.http.errors import AppError
    from app.modules.catalog.models import Product
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, ProductIngredient
    from app.modules.stock.schemas import StockRecipeLineCreate

    session = RecipeServiceSession(
        targets={("Product", 100): Product(id=100, name="Margherita", base_price=10, is_active=True)},
        ingredients={1: Ingredient(id=1, name="Pate", unit="piece", current_qty=10, alert_threshold=2)},
        recipes=[ProductIngredient(id=7, product_id=100, ingredient_id=1, quantity=1)],
    )

    with pytest.raises(AppError) as exc_info:
        await service.create_recipe_line(
            session,
            "product",
            100,
            StockRecipeLineCreate(ingredient_id=1, quantity=1.5),
        )

    assert exc_info.value.code == "DUPLICATE_RECIPE_INGREDIENT"
    assert exc_info.value.status_code == 409
    assert session.committed is False
