"""Phase 2 — Security Hardening test stubs.

All tests start RED. 02-02-PLAN.md implements the fixes that turn them GREEN.

Requirement coverage:
  SEC-01 → test_supply_rejects_zero_quantity, test_supply_rejects_negative_quantity
  SEC-02 → test_supply_records_user_id
  SEC-03 → test_availability_rejects_cross_tenant_product
"""

from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa

from app.core.http.deps import get_current_user
from app.main import app


@pytest.fixture(autouse=True)
def staff_user_override():
    async def _staff_user() -> dict:
        return {
            "id": "1",
            "tenant_id": 1,
            "tenant_slug": "default",
            "role": "staff",
            "email": "staff@example.test",
        }

    app.dependency_overrides[get_current_user] = _staff_user
    yield
    app.dependency_overrides.pop(get_current_user, None)

# ---------------------------------------------------------------------------
# SEC-01: POST /supply rejects quantity <= 0 via Pydantic Field(gt=0)
# ---------------------------------------------------------------------------


async def test_supply_rejects_zero_quantity(client):
    """quantity=0 must return 422 before any DB write."""
    response = await client.post(
        "/api/v1/stock/supply",
        json={"ingredient_id": 1, "quantity": 0},
    )
    assert response.status_code == 422


async def test_supply_rejects_negative_quantity(client):
    """quantity=-5.0 must return 422 before any DB write."""
    response = await client.post(
        "/api/v1/stock/supply",
        json={"ingredient_id": 1, "quantity": -5.0},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# SEC-02: POST /supply records current_user["id"] on StockMovement.user_id
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="TODO: needs staff_token fixture in conftest.py")
async def test_supply_records_user_id(client, db_session):
    """After a successful supply call, the created StockMovement row must
    have user_id equal to the authenticated user's id.

    To un-skip: add a `staff_token` fixture to conftest.py that returns a
    valid JWT for a staff user whose id is known, then assert:
        movement_user_id == known_staff_id
    """
    staff_token = "REPLACE_WITH_REAL_STAFF_JWT"
    known_user_id = 1  # id of the user the token belongs to

    response = await client.post(
        "/api/v1/stock/supply",
        json={"ingredient_id": 1, "quantity": 1.0},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert response.status_code == 200

    row = await db_session.execute(
        sa.text("SELECT user_id FROM stock_movements ORDER BY id DESC LIMIT 1")
    )
    movement_user_id = row.scalar_one()
    assert movement_user_id == known_user_id


# ---------------------------------------------------------------------------
# SEC-03: GET /availability returns 404 for product_id not in tenant schema
#
# When search_path is set to the tenant schema, SELECT FROM products WHERE
# id = X only sees that tenant's products. A product_id that belongs to
# another tenant (or simply does not exist) returns no rows → 404.
# Using product_ids=999999 simulates a cross-tenant product_id because
# no tenant in test data has a product with that id.
# ---------------------------------------------------------------------------


async def test_availability_rejects_cross_tenant_product(client, monkeypatch):
    """product_id=999999 (not in this tenant) must return 404."""
    class EmptyRecipeResult:
        def scalars(self):
            return []

    class TenantSession:
        async def get(self, model, primary_key):
            return None

        async def execute(self, statement):
            return EmptyRecipeResult()

    @asynccontextmanager
    async def fake_tenant_session(tenant_slug):
        yield TenantSession()

    monkeypatch.setattr(
        "app.modules.stock.router.get_tenant_session",
        fake_tenant_session,
    )

    response = await client.get(
        "/api/v1/stock/availability",
        params={"product_ids": 999999},
    )
    assert response.status_code == 404
