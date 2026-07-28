"""Phase 3 — Stock API endpoints and worker tests.

Requirement coverage:
  API-01 → test_movements_returns_paginated_filtered_items
  API-02 → test_alerts_returns_only_below_threshold
  API-03 → test_ingredients_filters_and_flags_threshold
  API-04 → test_patch_ingredient_updates_known_fields_only
  API-05 → test_adjust_inventory_sets_absolute_qty_and_records_delta
  API-05 → test_adjust_non_inventory_records_direct_delta
  API-06 → test_adjust_rejects_invalid_reason
  API-07 → test_send_stock_alert_uses_runtime_tenant_cooldown
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.http.deps import get_current_user
from app.main import app


@pytest.fixture(autouse=True)
def staff_user_override():
    async def _staff_user() -> dict:
        return {
            "id": "7",
            "tenant_id": 1,
            "tenant_slug": "default",
            "role": "staff",
            "email": "staff@example.test",
        }

    app.dependency_overrides[get_current_user] = _staff_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _admin_override():
    async def _admin_user() -> dict:
        return {
            "id": "9",
            "tenant_id": 1,
            "tenant_slug": "default",
            "role": "admin",
            "email": "admin@example.test",
        }

    return _admin_user


async def test_movements_returns_paginated_filtered_items(client, monkeypatch):
    captured = {}

    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_list_movements(session, pagination, ingredient_id, date_from, date_to):
        captured["session"] = session
        captured["pagination"] = pagination
        captured["ingredient_id"] = ingredient_id
        captured["date_from"] = date_from
        captured["date_to"] = date_to
        return [
            {
                "id": 12,
                "ingredient_id": 3,
                "quantity_delta": 2.5,
                "reason": "supply",
                "user_id": 7,
                "created_at": datetime(2026, 6, 24, 8, 30, tzinfo=timezone.utc),
            }
        ], 1

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.list_movements", fake_list_movements)

    response = await client.get(
        "/api/v1/stock/movements",
        params={
            "page": 2,
            "page_size": 5,
            "ingredient_id": 3,
            "date_from": "2026-06-01T00:00:00Z",
            "date_to": "2026-06-30T23:59:59Z",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 2
    assert payload["page_size"] == 5
    assert payload["items"][0]["id"] == 12
    assert captured["ingredient_id"] == 3
    assert captured["date_from"].isoformat() == "2026-06-01T00:00:00+00:00"
    assert captured["date_to"].isoformat() == "2026-06-30T23:59:59+00:00"


async def test_alerts_returns_only_below_threshold(client, monkeypatch):
    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_list_alerts(_session):
        return [
            {
                "id": 1,
                "name": "Mozzarella",
                "unit": "kg",
                "current_qty": 1.0,
                "alert_threshold": 2.0,
                "is_below_threshold": True,
            }
        ]

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.list_alerts", fake_list_alerts)

    response = await client.get("/api/v1/stock/alerts")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "name": "Mozzarella",
            "unit": "kg",
            "current_qty": 1.0,
            "alert_threshold": 2.0,
            "is_below_threshold": True,
        }
    ]


async def test_ingredients_filters_and_flags_threshold(client, monkeypatch):
    captured = {}

    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_list_ingredients(session, pagination, below_threshold, unit, search):
        captured["session"] = session
        captured["pagination"] = pagination
        captured["below_threshold"] = below_threshold
        captured["unit"] = unit
        captured["search"] = search
        return [
            {
                "id": 4,
                "name": "Farine",
                "unit": "kg",
                "current_qty": 0.5,
                "alert_threshold": 1.0,
                "is_below_threshold": True,
            }
        ], 1

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.list_ingredients", fake_list_ingredients)

    response = await client.get(
        "/api/v1/stock/ingredients",
        params={"below_threshold": "true", "unit": "kg", "search": "far"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["is_below_threshold"] is True
    assert captured["below_threshold"] is True
    assert captured["unit"] == "kg"
    assert captured["search"] == "far"


async def test_patch_ingredient_updates_known_fields_only(client, monkeypatch):
    app.dependency_overrides[get_current_user] = _admin_override()

    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_patch_ingredient(_session, ingredient_id, data):
        assert ingredient_id == 8
        assert data == {"name": "Tomate", "alert_threshold": 3.5}
        return {
            "id": 8,
            "name": "Tomate",
            "unit": "kg",
            "current_qty": 6.0,
            "alert_threshold": 3.5,
            "is_below_threshold": False,
        }

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.patch_ingredient", fake_patch_ingredient)

    response = await client.patch(
        "/api/v1/stock/ingredients/8",
        json={"name": "Tomate", "alert_threshold": 3.5, "ignored": "value"},
    )

    assert response.status_code == 200
    assert response.json()["alert_threshold"] == 3.5


async def test_adjust_inventory_sets_absolute_qty_and_records_delta(client, monkeypatch):
    app.dependency_overrides[get_current_user] = _admin_override()

    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_adjust(_session, ingredient_id, quantity, reason, user_id):
        assert ingredient_id == 2
        assert quantity == 9.0
        assert reason == "inventory"
        assert user_id == 9
        return {
            "id": 2,
            "name": "Basilic",
            "unit": "kg",
            "current_qty": 9.0,
            "alert_threshold": 2.0,
            "is_below_threshold": False,
        }

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.adjust_ingredient_stock", fake_adjust)

    response = await client.post(
        "/api/v1/stock/ingredients/2/adjust",
        json={"reason": "inventory", "new_qty": 9.0},
    )

    assert response.status_code == 200
    assert response.json()["current_qty"] == 9.0


async def test_adjust_non_inventory_records_direct_delta(client, monkeypatch):
    app.dependency_overrides[get_current_user] = _admin_override()

    class FakeSession:
        pass

    @asynccontextmanager
    async def fake_tenant_session(_tenant_slug):
        yield FakeSession()

    async def fake_adjust(_session, ingredient_id, quantity, reason, user_id):
        assert ingredient_id == 2
        assert quantity == -1.5
        assert reason == "waste"
        assert user_id == 9
        return {
            "id": 2,
            "name": "Basilic",
            "unit": "kg",
            "current_qty": 3.5,
            "alert_threshold": 2.0,
            "is_below_threshold": False,
        }

    monkeypatch.setattr("app.modules.stock.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr("app.modules.stock.service.adjust_ingredient_stock", fake_adjust)

    response = await client.post(
        "/api/v1/stock/ingredients/2/adjust",
        json={"reason": "waste", "quantity": -1.5},
    )

    assert response.status_code == 200
    assert response.json()["current_qty"] == 3.5


async def test_adjust_rejects_invalid_reason(client):
    app.dependency_overrides[get_current_user] = _admin_override()

    response = await client.post(
        "/api/v1/stock/ingredients/2/adjust",
        json={"reason": "bad-value", "quantity": 1.0},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_send_stock_alert_uses_runtime_tenant_cooldown(monkeypatch):
    from worker.tasks import stock_alerts

    enqueue_calls = []
    notify_calls = []
    committed = {"value": False}
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    ingredient = SimpleNamespace(
        id=5,
        name="Levure",
        last_alert_sent_at=now - timedelta(hours=3),
    )
    config = SimpleNamespace(stock_alert_cooldown_hours=2)

    class FakeSession:
        async def execute(self, _statement):
            return None

        async def get(self, model, primary_key=None):
            if model.__name__ == "Ingredient":
                return ingredient
            if model.__name__ == "TenantConfig":
                return config
            raise AssertionError(model)

        async def commit(self):
            committed["value"] = True

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSessionFactory:
        def __call__(self):
            return FakeSessionContext()

    class FakeEngine:
        async def dispose(self):
            return None

    class FakeRedis:
        async def enqueue_job(self, name, **kwargs):
            enqueue_calls.append((name, kwargs))

    async def fake_notify_staff(**kwargs):
        notify_calls.append(kwargs)

    monkeypatch.setattr(stock_alerts, "create_async_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(stock_alerts, "async_sessionmaker", lambda *args, **kwargs: FakeSessionFactory())
    monkeypatch.setattr(stock_alerts, "notify_staff", fake_notify_staff, raising=False)
    monkeypatch.setattr(stock_alerts, "datetime", SimpleNamespace(now=lambda _tz: now))

    response_ctx = {"redis": FakeRedis()}

    await stock_alerts.send_stock_alert(
        response_ctx,
        ingredient_id=5,
        ingredient_name="Levure",
        current_qty=1.0,
        tenant_slug="default",
    )

    assert committed["value"] is True
    assert ingredient.last_alert_sent_at == now
    assert notify_calls
    assert enqueue_calls == [
        (
            "send_stock_alert_email",
            {
                "tenant_slug": "default",
                "ingredient_id": 5,
                "ingredient_name": "Levure",
                "current_qty": 1.0,
            },
        )
    ]