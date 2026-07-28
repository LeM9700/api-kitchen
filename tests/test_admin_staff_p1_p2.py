from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock


def test_batch_effective_expiration_uses_earliest_deadline():
    from app.modules.stock.models import IngredientBatch
    from app.modules.stock.service import _effective_batch_expires_at

    opened_at = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
    batch = IngredientBatch(
        ingredient_id=1,
        quantity=2,
        received_at=opened_at - timedelta(days=1),
        expires_at=opened_at + timedelta(days=3),
        opened_at=opened_at,
        use_within_hours_after_opening=24,
        status="opened",
    )

    assert _effective_batch_expires_at(batch) == opened_at + timedelta(hours=24)


async def test_admin_approval_applies_stock_adjustment_and_audit_movement():
    from app.modules.admin.tenants.models import TenantConfig
    from app.modules.stock import service
    from app.modules.stock.models import Ingredient, StockAdjustmentRequest, StockMovement

    ingredient = Ingredient(id=1, name="Pate", unit="kg", current_qty=10, alert_threshold=2)
    request = StockAdjustmentRequest(
        id=4,
        ingredient_id=1,
        quantity_delta=-3,
        reason="waste",
        status="pending",
        requested_by_user_id=7,
    )

    async def fake_get(model, primary_key):
        if model is StockAdjustmentRequest:
            return request
        if model is Ingredient:
            return ingredient
        return None

    session = AsyncMock()
    session.get = AsyncMock(side_effect=fake_get)
    session.scalar = AsyncMock(return_value=TenantConfig(large_stock_adjustment_threshold=2))
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    payload = await service.approve_adjustment_request(session, 4, user_id=9, note="Controle")

    movement = session.add.call_args.args[0]
    assert isinstance(movement, StockMovement)
    assert float(ingredient.current_qty) == 7
    assert movement.reason == "request:waste"
    assert movement.user_id == 9
    assert payload["status"] == "approved"
    assert payload["reviewed_by_user_id"] == 9
    assert payload["is_large_adjustment"] is True


async def test_reject_adjustment_request_leaves_stock_unchanged():
    from app.modules.admin.tenants.models import TenantConfig
    from app.modules.stock import service
    from app.modules.stock.models import StockAdjustmentRequest

    request = StockAdjustmentRequest(
        id=5,
        ingredient_id=1,
        quantity_delta=-1,
        reason="loss",
        status="pending",
        requested_by_user_id=7,
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=request)
    session.scalar = AsyncMock(return_value=TenantConfig(large_stock_adjustment_threshold=10))
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    payload = await service.reject_adjustment_request(session, 5, user_id=9, note="Refuse")

    session.add.assert_not_called()
    assert payload["status"] == "rejected"
    assert payload["is_large_adjustment"] is False


async def test_orders_export_csv_contains_admin_staff_fields():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(
        id=10,
        customer_email="client@example.test",
        customer_name="Client comptoir",
        customer_phone="0600000000",
        order_type="dine_in",
        status="confirmed",
        payment_status="paid",
        source="manual",
        table_number="12",
        subtotal=12,
        discount_total=0,
        delivery_fee=0,
        total=12,
        created_at=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
    )

    class Result:
        def scalars(self):
            return [order]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=Result())

    csv_text = await service.export_orders_csv(session, order_type="dine_in")

    assert "customer_name" in csv_text
    assert "Client comptoir" in csv_text
    assert "dine_in" in csv_text


async def test_payments_export_csv_contains_order_filters_fields():
    from app.modules.orders.models import Order
    from app.modules.payments import service
    from app.modules.payments.models import Payment

    payment = Payment(
        id=3,
        order_id=10,
        provider="cash",
        amount=12,
        amount_received=20,
        currency="EUR",
        status="paid",
        created_by_user_id=9,
        created_at=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
    )
    order = Order(id=10, order_type="dine_in", status="confirmed", payment_status="paid")

    class Result:
        def all(self):
            return [(payment, order)]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=Result())

    csv_text = await service.export_payments_csv(session, provider="cash", order_type="dine_in")

    assert "amount_received" in csv_text
    assert "cash" in csv_text
    assert "dine_in" in csv_text


async def test_terminal_intent_uses_local_fallback_when_stripe_is_unavailable(monkeypatch):
    from app.modules.orders.models import Order
    from app.modules.payments import service

    order = Order(id=10, total=15, payment_status="pending")
    added: list[object] = []

    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock(side_effect=added.append)

    async def fake_flush():
        added[-1].id = 55

    session.flush = AsyncMock(side_effect=fake_flush)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    async def fake_context(_session, _tenant_slug):
        return service.StripeContext()

    monkeypatch.setattr(service, "get_stripe_context", fake_context)
    monkeypatch.setattr(service, "_local_fallback_allowed", lambda: True)
    monkeypatch.setattr(service.stripe.PaymentIntent, "create", MagicMock(side_effect=RuntimeError("offline")))

    result = await service.create_terminal_intent(session, 10, "default", user_id=9)

    assert result["payment"].id == 55
    assert result["payment"].provider == "stripe_terminal"
    assert result["client_secret"] == "local_terminal_55"


def test_fine_permissions_are_backward_compatible_and_authoritative():
    from app.core.http.deps import has_permission

    assert has_permission({"role": "admin", "permissions": []}, "orders:write") is True
    assert has_permission({"role": "staff", "permissions": None}, "orders:write") is True
    assert has_permission({"role": "staff", "permissions": ["orders:read"]}, "orders:read") is True
    assert has_permission({"role": "staff", "permissions": ["orders:read"]}, "orders:write") is False


def test_p1_p2_openapi_paths_are_registered():
    from app.main import app

    paths = set(app.openapi()["paths"])

    assert "/api/v1/stock/ingredients/{ingredient_id}/batches" in paths
    assert "/api/v1/stock/adjustment-requests" in paths
    assert "/api/v1/orders/export/csv" in paths
    assert "/api/v1/payments/export/csv" in paths
    assert "/api/v1/payments/terminal/connection-token" in paths
    assert "/api/v1/tenant/print-config" in paths


def test_admin_user_permissions_schema_accepts_explicit_list():
    from app.modules.admin.users.schemas import AdminUserPermissionsUpdate

    body = AdminUserPermissionsUpdate(permissions=["orders:read", "payments:terminal"])

    assert body.permissions == ["orders:read", "payments:terminal"]
