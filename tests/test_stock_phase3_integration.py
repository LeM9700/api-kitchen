from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.http.deps import get_current_user
from app.main import app
from app.modules.stock.models import Ingredient, StockMovement


@pytest.fixture
async def integration_db_session():
    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


@pytest.fixture
def auth_user_override():
    current_user = {
        "id": "17",
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


@pytest.fixture
def tenant_session_override(monkeypatch, integration_db_session):
    @asynccontextmanager
    async def _tenant_session(_tenant_slug: str):
        yield integration_db_session

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", _tenant_session)
    return integration_db_session


async def test_stock_movements_endpoint_filters_by_ingredient_and_date(
    client,
    tenant_session_override,
    auth_user_override,
):
    db_session = tenant_session_override
    ing_a = Ingredient(name="Farine T45", unit="kg", current_qty=20, alert_threshold=5)
    ing_b = Ingredient(name="Levure", unit="g", current_qty=800, alert_threshold=100)
    db_session.add_all([ing_a, ing_b])
    await db_session.flush()

    older = datetime.now(timezone.utc) - timedelta(days=5)
    inside = datetime.now(timezone.utc) - timedelta(days=1)
    newer = datetime.now(timezone.utc)
    db_session.add_all(
        [
            StockMovement(
                ingredient_id=ing_a.id,
                quantity_delta=3,
                reason="supply",
                user_id=17,
                created_at=older,
            ),
            StockMovement(
                ingredient_id=ing_a.id,
                quantity_delta=-1,
                reason="waste",
                user_id=17,
                created_at=inside,
            ),
            StockMovement(
                ingredient_id=ing_b.id,
                quantity_delta=2,
                reason="correction",
                user_id=17,
                created_at=newer,
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        "/api/v1/stock/movements",
        params={
            "ingredient_id": ing_a.id,
            "date_from": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["ingredient_id"] == ing_a.id
    assert payload["items"][0]["reason"] == "waste"


async def test_stock_alerts_and_ingredient_filters_hit_real_db(
    client,
    tenant_session_override,
    auth_user_override,
):
    db_session = tenant_session_override
    db_session.add_all(
        [
            Ingredient(name="Mozzarella", unit="kg", current_qty=1, alert_threshold=2),
            Ingredient(name="Tomate", unit="kg", current_qty=6, alert_threshold=2),
            Ingredient(name="Basilic", unit="g", current_qty=20, alert_threshold=50),
        ]
    )
    await db_session.commit()

    alerts_response = await client.get("/api/v1/stock/alerts")
    assert alerts_response.status_code == 200
    alert_names = {item["name"] for item in alerts_response.json()}
    assert alert_names == {"Mozzarella", "Basilic"}

    ingredients_response = await client.get(
        "/api/v1/stock/ingredients",
        params={"below_threshold": "true", "unit": "kg", "search": "zza"},
    )
    assert ingredients_response.status_code == 200
    payload = ingredients_response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "Mozzarella"
    assert payload["items"][0]["is_below_threshold"] is True


async def test_patch_ingredient_updates_only_allowed_fields_on_db(
    client,
    tenant_session_override,
    auth_user_override,
):
    db_session = tenant_session_override
    auth_user_override["role"] = "admin"
    ingredient = Ingredient(name="Sauce", unit="L", current_qty=4, alert_threshold=1)
    db_session.add(ingredient)
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/stock/ingredients/{ingredient.id}",
        json={"name": "Sauce tomate", "alert_threshold": 2.5, "current_qty": 999},
    )

    assert response.status_code == 200
    await db_session.refresh(ingredient)
    assert ingredient.name == "Sauce tomate"
    assert float(ingredient.alert_threshold) == 2.5
    assert float(ingredient.current_qty) == 4


async def test_adjust_inventory_and_waste_persist_stock_and_movements(
    client,
    tenant_session_override,
    auth_user_override,
):
    db_session = tenant_session_override
    auth_user_override["role"] = "admin"
    ingredient = Ingredient(name="Olive", unit="kg", current_qty=5, alert_threshold=1)
    db_session.add(ingredient)
    await db_session.commit()

    inventory_response = await client.post(
        f"/api/v1/stock/ingredients/{ingredient.id}/adjust",
        json={"reason": "inventory", "new_qty": 8},
    )
    assert inventory_response.status_code == 200

    waste_response = await client.post(
        f"/api/v1/stock/ingredients/{ingredient.id}/adjust",
        json={"reason": "waste", "quantity": -1.5},
    )
    assert waste_response.status_code == 200

    await db_session.refresh(ingredient)
    assert float(ingredient.current_qty) == 6.5

    movement_rows = await db_session.execute(
        sa.text(
            "SELECT quantity_delta, reason, user_id FROM stock_movements "
            "WHERE ingredient_id = :ingredient_id ORDER BY id"
        ),
        {"ingredient_id": ingredient.id},
    )
    movements = movement_rows.fetchall()
    assert [float(row.quantity_delta) for row in movements] == [3.0, -1.5]
    assert [row.reason for row in movements] == ["inventory", "waste"]
    assert {row.user_id for row in movements} == {17}