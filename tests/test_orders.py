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
    body = OrderCreate(order_type="pickup", items=[OrderItemCreate(product_id=1, quantity=1)])

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
        order_type="pickup",
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
        order_type="pickup",
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


async def test_update_item_preparation_marks_ready_with_audit_fields():
    from app.modules.orders import service
    from app.modules.orders.models import Order, OrderItem

    order = Order(id=1, status="preparing", payment_status="paid", total=10)
    item = OrderItem(
        id=5,
        order_id=1,
        product_id=2,
        quantity=1,
        unit_price=10,
        total=10,
        preparation_status="pending",
        preparation_station="kitchen",
    )
    session = AsyncMock()
    session.get = AsyncMock(side_effect=[order, item])
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    payload = await service.update_item_preparation(
        session,
        order_id=1,
        item_id=5,
        status="ready",
        note="Pret cuisine",
        actor_user_id=9,
    )

    assert item.preparation_status == "ready"
    assert item.prepared_by_user_id == 9
    assert item.prepared_at is not None
    assert payload["preparation_station"] == "kitchen"
    session.commit.assert_awaited_once()


async def test_update_item_preparation_rejects_terminal_order():
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    session = AsyncMock()
    session.get = AsyncMock(return_value=Order(id=1, status="delivered", total=10))

    with pytest.raises(AppError) as exc_info:
        await service.update_item_preparation(
            session,
            order_id=1,
            item_id=5,
            status="ready",
            note=None,
            actor_user_id=9,
        )

    assert exc_info.value.code == "ORDER_NOT_ACTIVE"


def _mock_session(order):
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


async def test_internal_authority_valid_transition_persists_without_warning():
    """INTERNAL + transition valide : comportement inchangé, authority='internal' persisté."""
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="preparing", payment_status="paid", total=10)
    session = _mock_session(order)

    with patch.object(service.logger, "warning") as mock_warning:
        result = await service.update_status(session, 1, "ready", tenant_slug="acme")

    assert result.status == "ready"
    mock_warning.assert_not_called()
    history_entry = session.add.call_args_list[0].args[0]
    assert history_entry.status == "ready"
    assert history_entry.authority == service.TransitionAuthority.INTERNAL.value


async def test_internal_authority_invalid_transition_rejected_422():
    """INTERNAL + transition hors VALID_TRANSITIONS : rejet 422 inchangé."""
    from app.core.http.errors import AppError
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="delivered", payment_status="paid", total=10)
    session = _mock_session(order)

    with pytest.raises(AppError) as exc_info:
        await service.update_status(session, 1, "confirmed", tenant_slug="acme")

    assert exc_info.value.code == "INVALID_STATUS_TRANSITION"
    assert exc_info.value.status_code == 422
    session.commit.assert_not_called()


async def test_external_authority_valid_transition_persists_without_warning():
    """EXTERNAL + transition valide : persistée, authority='external', pas de warning."""
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="preparing", payment_status="paid", total=10)
    session = _mock_session(order)

    with patch.object(service.logger, "warning") as mock_warning:
        result = await service.update_status(
            session, 1, "ready", tenant_slug="acme", authority=service.TransitionAuthority.EXTERNAL
        )

    assert result.status == "ready"
    mock_warning.assert_not_called()
    history_entry = session.add.call_args_list[0].args[0]
    assert history_entry.status == "ready"
    assert history_entry.authority == service.TransitionAuthority.EXTERNAL.value


async def test_external_authority_out_of_graph_transition_never_rejected():
    """EXTERNAL + ready->cancelled (hors VALID_TRANSITIONS) : jamais de 422.

    Persistée quand même, avec un warning loggé et le compteur métrique dédié
    incrémenté -- la transition reste observable sans jamais être bloquée.
    """
    from app.modules.orders import service
    from app.modules.orders.models import Order

    assert "cancelled" not in service.VALID_TRANSITIONS["ready"]

    order = Order(id=1, status="ready", payment_status="paid", total=10)
    session = _mock_session(order)

    counter_before = service.external_out_of_graph_transitions_total.value
    with patch.object(service.logger, "warning") as mock_warning:
        result = await service.update_status(
            session, 1, "cancelled", tenant_slug="acme", authority=service.TransitionAuthority.EXTERNAL
        )

    assert result.status == "cancelled"
    mock_warning.assert_called_once()
    assert service.external_out_of_graph_transitions_total.value == counter_before + 1
    history_entry = session.add.call_args_list[0].args[0]
    assert history_entry.status == "cancelled"
    assert history_entry.authority == service.TransitionAuthority.EXTERNAL.value


async def test_internal_authority_supports_rejected_and_delivery_failed_statuses():
    """Les statuts d'interopérabilité rejected/delivery_failed sont des transitions valides."""
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="pending", payment_status="pending", total=10)
    session = _mock_session(order)
    result = await service.update_status(session, 1, "rejected", tenant_slug="acme")
    assert result.status == "rejected"

    order2 = Order(id=2, status="out_for_delivery", payment_status="paid", total=10)
    session2 = _mock_session(order2)
    result2 = await service.update_status(session2, 2, "delivery_failed", tenant_slug="acme")
    assert result2.status == "delivery_failed"
