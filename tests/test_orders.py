from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def test_list_orders_requires_staff(client):
    response = await client.get("/api/v1/orders")
    assert response.status_code == 401


async def test_confirm_order_rolls_back_on_insufficient_stock():
    """Si deduct_for_order lève INSUFFICIENT_STOCK, la commande ne doit pas passer en confirmed.

    [⚠️ PROD] Vérifie l'atomicité : session.commit() ne doit jamais être appelé
    si la déduction de stock échoue. Sans cette garantie, la commande serait
    marquée "confirmed" en base mais le stock resterait intact.
    """
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="pending", payment_status="paid", total=50)

    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    with patch(
        "app.modules.orders.service.deduct_for_order",
        new_callable=AsyncMock,
        side_effect=AppError("INSUFFICIENT_STOCK", "Not enough stock for Tomato", 409),
    ):
        with pytest.raises(AppError) as exc_info:
            await service.update_status(session, 1, "confirmed", tenant_slug="acme")

    assert exc_info.value.code == "INSUFFICIENT_STOCK"
    session.commit.assert_not_called()


async def test_confirm_order_requires_paid_payment_status():
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="pending", payment_status="pending", total=50)

    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.commit = AsyncMock()

    with pytest.raises(AppError) as exc_info:
        await service.update_status(session, 1, "confirmed", tenant_slug="acme")

    assert exc_info.value.code == "PAYMENT_REQUIRED"
    session.commit.assert_not_called()


async def test_client_cancel_only_allows_owner_pending_order():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, user_id=7, status="pending", total=50)
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)

    with patch("app.modules.orders.service.update_status", new_callable=AsyncMock) as update_status:
        update_status.return_value = order
        result = await service.cancel_my_order(session, 1, 7, "acme")

    assert result is order
    update_status.assert_awaited_once()
    assert update_status.await_args.args[:4] == (session, 1, "cancelled", "Cancelled by customer")


async def test_client_cannot_read_someone_elses_order_detail():
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    session = AsyncMock()
    session.get = AsyncMock(return_value=Order(id=1, user_id=7, status="pending", total=50))

    with pytest.raises(AppError) as exc_info:
        await service.get_order_detail(session, 1, user_id=8, is_staff=False)

    assert exc_info.value.code == "ORDER_NOT_FOUND"


async def test_create_order_requires_idempotency_key():
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    session = AsyncMock()
    body = OrderCreate(items=[OrderItemCreate(product_id=1, quantity=1)])

    with pytest.raises(AppError) as exc_info:
        await service.create_order(session, body, user_id=1)

    assert exc_info.value.code == "IDEMPOTENCY_KEY_REQUIRED"


async def test_create_order_ignores_client_delivery_fee_without_zone():
    from app.modules.catalog.models import Product
    from app.modules.orders import service
    from app.modules.orders.models import Order
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    product = Product(id=1, name="Margherita", base_price=10, is_active=True)
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[None, None])
    session.get = AsyncMock(return_value=product)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    body = OrderCreate(
        delivery_fee=999,
        items=[OrderItemCreate(product_id=1, quantity=2)],
    )
    order = await service.create_order(session, body, user_id=1, idempotency_key="abc")

    assert isinstance(order, Order)
    assert float(order.delivery_fee) == 0
    assert float(order.total) == 20


async def test_create_order_prices_allowed_extras_server_side():
    from app.modules.catalog.models import Extra, Product
    from app.modules.orders import service
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate, OrderItemExtraCreate

    product = Product(id=1, name="Margherita", base_price=10, is_active=True)
    extra = Extra(id=5, name="Mozzarella", price=2.5, is_active=True)
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[None, extra, None])
    session.get = AsyncMock(return_value=product)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    body = OrderCreate(
        items=[
            OrderItemCreate(
                product_id=1,
                quantity=2,
                extras=[OrderItemExtraCreate(extra_id=5, quantity=2)],
            )
        ],
    )
    order = await service.create_order(session, body, user_id=1, idempotency_key="abc")

    assert float(order.subtotal) == 30
    added_item = session.add.call_args_list[1].args[0]
    assert float(added_item.extras_total) == 10
    assert added_item.extras_snapshot[0]["name"] == "Mozzarella"
