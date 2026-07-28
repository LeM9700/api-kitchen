"""Tests pour order_type (delivery | pickup) sur les commandes.

Suit les conventions de tests/test_orders.py : pas de fixtures HTTP/DB integrees
(authed_client, demo_tenant... n'existent pas dans ce projet), mais des tests
unitaires appelant directement le schema Pydantic ou service.create_order avec
une AsyncSession mockee (AsyncMock).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError


def test_pickup_order_does_not_require_delivery_address():
    """Une commande pickup est valide sans delivery_address."""
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    body = OrderCreate(
        order_type="pickup",
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    assert body.order_type == "pickup"
    assert body.delivery_address is None


def test_dine_in_order_does_not_require_delivery_address():
    """Une commande sur place est valide sans adresse de livraison."""
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    body = OrderCreate(
        order_type="dine_in",
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    assert body.order_type == "dine_in"
    assert body.delivery_address is None


def test_delivery_order_requires_delivery_address():
    """Une commande delivery sans delivery_address est rejetee par Pydantic (422 cote API)."""
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    with pytest.raises(ValidationError) as exc_info:
        OrderCreate(
            order_type="delivery",
            items=[OrderItemCreate(product_id=1, quantity=1)],
        )
    assert "delivery_address" in str(exc_info.value)


def test_delivery_order_with_address_is_valid():
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    body = OrderCreate(
        order_type="delivery",
        delivery_address="1 rue de la Paix",
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    assert body.order_type == "delivery"


def test_order_type_defaults_to_delivery_for_backward_compatibility():
    """Un client existant qui n'envoie pas order_type -- comportement identique a avant ce plan."""
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    body = OrderCreate(
        delivery_address="1 rue de la Paix",
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    assert body.order_type == "delivery"


def test_invalid_order_type_rejected():
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    with pytest.raises(ValidationError):
        OrderCreate(
            order_type="drive",
            delivery_address="1 rue de la Paix",
            items=[OrderItemCreate(product_id=1, quantity=1)],
        )


async def test_create_pickup_order_has_no_delivery_fee_and_no_address():
    """order_type=pickup -> delivery_fee=0, delivery_address ignore meme si envoye."""
    from app.modules.catalog.models import Product
    from app.modules.orders import service
    from app.modules.orders.models import Order
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    product = Product(id=1, name="Margherita", base_price=10, is_active=True)
    session = AsyncMock()
    # 1) idempotency dedupe check -> None  2) TenantConfig lookup (_estimate_delivery_at) -> None
    session.scalar = AsyncMock(side_effect=[None, None])
    session.get = AsyncMock(return_value=product)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    body = OrderCreate(
        order_type="pickup",
        delivery_address="adresse envoyee par erreur",
        items=[OrderItemCreate(product_id=1, quantity=2)],
    )
    order = await service.create_order(session, body, user_id=1, idempotency_key="abc")

    assert isinstance(order, Order)
    assert order.order_type == "pickup"
    assert float(order.delivery_fee) == 0
    assert order.delivery_address is None
    assert order.delivery_zone_id is None
    # session.get n'est jamais appele pour une zone de livraison en pickup.
    session.get.assert_called_with(Product, 1)


async def test_create_pickup_order_does_not_look_up_delivery_zone():
    """order_type=pickup ignore delivery_zone_id meme s'il est envoye -- pas de lookup DeliveryZone."""
    from app.modules.catalog.models import Product
    from app.modules.orders import service
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
        delivery_zone_id=999,
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    order = await service.create_order(session, body, user_id=1, idempotency_key="abc")

    assert order.delivery_zone_id is None
    # Seul le produit est recupere via session.get -- jamais DeliveryZone.
    session.get.assert_called_once_with(Product, 1)


async def test_create_dine_in_order_has_no_delivery_fee_and_no_zone():
    """order_type=dine_in -> pas de frais, pas de zone, adresse ignoree."""
    from app.modules.catalog.models import Product
    from app.modules.orders import service
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
        order_type="dine_in",
        delivery_address="adresse envoyee par erreur",
        delivery_zone_id=999,
        items=[OrderItemCreate(product_id=1, quantity=1)],
    )
    order = await service.create_order(session, body, user_id=1, idempotency_key="abc")

    assert order.order_type == "dine_in"
    assert float(order.delivery_fee) == 0
    assert order.delivery_address is None
    assert order.delivery_zone_id is None


def test_manual_order_schema_accepts_cash_dine_in_without_customer_account():
    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    body = ManualOrderCreate(
        order_type="dine_in",
        customer={"full_name": "Client comptoir"},
        table_number="12",
        items=[OrderItemCreate(product_id=1, quantity=1)],
        payment={"method": "cash", "amount_received": 20},
    )

    assert body.customer_email is None
    assert body.customer.full_name == "Client comptoir"
    assert body.payment.method == "cash"


def test_manual_terminal_payment_requires_external_reference():
    import pytest
    from pydantic import ValidationError

    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    with pytest.raises(ValidationError) as exc_info:
        ManualOrderCreate(
            order_type="pickup",
            items=[OrderItemCreate(product_id=1, quantity=1)],
            payment={"method": "external_terminal"},
        )

    assert "external_reference" in str(exc_info.value)


def test_serialized_order_list_includes_order_type():
    """La sortie API (OrderListOut/OrderDetailOut) expose order_type au client."""
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, order_type="pickup", status="pending", total=10)
    payload = service._serialize_order_list(order)
    assert payload["order_type"] == "pickup"


def test_serialized_order_list_defaults_order_type_to_delivery():
    """Une commande existante sans order_type explicite (avant migration) -- traitee comme delivery."""
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="pending", total=10)
    order.order_type = None  # simule une ligne pre-migration jamais rafraichie depuis la DB
    payload = service._serialize_order_list(order)
    assert payload["order_type"] == "delivery"
