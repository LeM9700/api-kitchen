"""Phase 0 du chantier livraison interne (docs/superpowers/specs/2026-10-01-livraison-interne-plan-sprint.md).

Couvre : coordonnees GPS obligatoires pour une livraison (zone retrouvee cote serveur),
telephone obligatoire, motif d'echec de livraison, notification, remboursement, 404 sur
zone inconnue et verrou de ligne sur update_status.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from app.core.http.errors import AppError

# Carre [0,1] x [0,1] puis [2,3] x [2,3], en GeoJSON (ordre [lng, lat]).
_SQUARE_A = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
_SQUARE_B = {"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]]}


async def _seed(session, *, min_order_a: float = 0):
    from app.modules.catalog.models import Product
    from app.modules.delivery.models import DeliveryZone
    from app.modules.hr.models import Establishment

    await session.execute(sa.text("UPDATE establishments SET is_active = false"))
    await session.execute(sa.text("UPDATE delivery_zones SET is_active = false"))
    session.add(Establishment(id=9101, name="Resto livraison", timezone="Europe/Paris", is_active=True))
    product = Product(name="Pizza livraison", base_price=12, is_active=True)
    zone_a = DeliveryZone(
        name="Zone A", polygon=_SQUARE_A, fee=3, min_order_amount=min_order_a, estimated_minutes=20
    )
    zone_b = DeliveryZone(name="Zone B", polygon=_SQUARE_B, fee=9, min_order_amount=0, estimated_minutes=40)
    session.add_all([product, zone_a, zone_b])
    await session.flush()
    return product, zone_a, zone_b


def _body(product, **overrides):
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    data = {
        "order_type": "delivery",
        "delivery_address": "1 rue de la Paix",
        "customer_phone": "+33 6 12 34 56 78",
        "items": [OrderItemCreate(product_id=product.id, quantity=1)],
    }
    data.update(overrides)
    return OrderCreate(**data)


# --- Schema -----------------------------------------------------------------


def test_schema_requires_lat_and_lng_together():
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    with pytest.raises(ValidationError) as exc:
        OrderCreate(
            delivery_address="x",
            delivery_lat=48.85,
            items=[OrderItemCreate(product_id=1, quantity=1)],
        )
    assert "delivery_lat et delivery_lng" in str(exc.value)


def test_schema_rejects_out_of_range_coordinates():
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    with pytest.raises(ValidationError):
        OrderCreate(
            delivery_address="x",
            delivery_lat=123,
            delivery_lng=2.3,
            items=[OrderItemCreate(product_id=1, quantity=1)],
        )


def test_schema_rejects_invalid_phone_and_trims_instructions():
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    item = [OrderItemCreate(product_id=1, quantity=1)]
    with pytest.raises(ValidationError):
        OrderCreate(delivery_address="x", customer_phone="pas un numero", items=item)

    body = OrderCreate(
        delivery_address="x",
        customer_phone="  +33 6 12 34 56 78 ",
        delivery_instructions="  3e etage, code 4521  ",
        items=item,
    )
    assert body.customer_phone == "+33 6 12 34 56 78"
    assert body.delivery_instructions == "3e etage, code 4521"


# --- Livraison : coordonnees, zone, telephone -------------------------------


async def test_customer_delivery_without_coordinates_is_refused(db_session):
    from app.modules.orders import service

    product, zone_a, _ = await _seed(db_session)
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session,
            _body(product, delivery_zone_id=zone_a.id),
            user_id=1,
            idempotency_key="p0-nocoords",
        )
    assert exc.value.code == "DELIVERY_COORDINATES_REQUIRED"
    assert exc.value.status_code == 422


async def test_customer_delivery_requires_phone(db_session):
    from app.modules.orders import service

    product, _, _ = await _seed(db_session)
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session,
            _body(product, customer_phone=None, delivery_lat=0.5, delivery_lng=0.5),
            user_id=1,
            idempotency_key="p0-nophone",
        )
    assert exc.value.code == "CUSTOMER_PHONE_REQUIRED"


async def test_zone_is_resolved_from_coordinates_and_client_zone_id_is_ignored(db_session):
    from app.modules.orders import service

    product, zone_a, zone_b = await _seed(db_session)
    order = await service.create_order(
        db_session,
        # Le client pretend etre en zone B (frais 9) alors que le point est en zone A (frais 3).
        _body(
            product,
            delivery_zone_id=zone_b.id,
            delivery_lat=0.5,
            delivery_lng=0.5,
            delivery_instructions="Interphone 12",
        ),
        user_id=1,
        idempotency_key="p0-zone-from-coords",
    )
    assert order.delivery_zone_id == zone_a.id
    assert float(order.delivery_fee) == 3
    assert float(order.total) == 15
    assert order.delivery_lat == 0.5 and order.delivery_lng == 0.5
    assert order.delivery_instructions == "Interphone 12"
    assert order.customer_phone == "+33 6 12 34 56 78"


async def test_coordinates_outside_every_zone_are_refused(db_session):
    from app.modules.orders import service

    product, _, _ = await _seed(db_session)
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session,
            _body(product, delivery_lat=10, delivery_lng=10),
            user_id=1,
            idempotency_key="p0-outside",
        )
    assert exc.value.code == "DELIVERY_ZONE_UNREACHABLE"


async def test_minimum_order_still_enforced_with_coordinates(db_session):
    from app.modules.orders import service

    product, _, _ = await _seed(db_session, min_order_a=50)
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session,
            _body(product, delivery_lat=0.5, delivery_lng=0.5),
            user_id=1,
            idempotency_key="p0-min",
        )
    assert exc.value.code == "DELIVERY_MIN_ORDER_NOT_MET"


async def test_manual_order_can_pick_zone_without_coordinates(db_session):
    from app.modules.orders import service

    product, zone_a, _ = await _seed(db_session)
    order = await service.create_order(
        db_session,
        _body(product, delivery_zone_id=zone_a.id),
        user_id=None,
        idempotency_key="p0-manual-zone",
        source="manual",
        created_by_user_id=1,
    )
    assert order.delivery_zone_id == zone_a.id
    assert float(order.delivery_fee) == 3
    assert order.delivery_lat is None


async def test_manual_delivery_without_zone_or_coordinates_is_refused(db_session):
    from app.modules.orders import service

    product, _, _ = await _seed(db_session)
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session,
            _body(product),
            user_id=None,
            idempotency_key="p0-manual-none",
            source="manual",
            created_by_user_id=1,
        )
    assert exc.value.code == "DELIVERY_ZONE_REQUIRED"


async def test_pickup_never_stores_delivery_data(db_session):
    from app.modules.orders import service

    product, zone_a, _ = await _seed(db_session)
    order = await service.create_order(
        db_session,
        _body(
            product,
            order_type="pickup",
            delivery_zone_id=zone_a.id,
            delivery_lat=0.5,
            delivery_lng=0.5,
            delivery_instructions="ignore",
            customer_phone=None,
        ),
        user_id=1,
        idempotency_key="p0-pickup",
    )
    assert order.delivery_zone_id is None
    assert order.delivery_lat is None and order.delivery_instructions is None
    assert float(order.delivery_fee) == 0


async def test_overlapping_zones_resolve_to_lowest_id(db_session):
    from app.modules.delivery import service as delivery_service
    from app.modules.delivery.models import DeliveryZone

    _, zone_a, _ = await _seed(db_session)
    overlap = DeliveryZone(name="Zone A bis", polygon=_SQUARE_A, fee=1, estimated_minutes=10)
    db_session.add(overlap)
    await db_session.flush()

    zone = await delivery_service.check_address(db_session, 0.5, 0.5)
    assert zone.id == zone_a.id


# --- Echec de livraison -----------------------------------------------------


def _mock_session(order):
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


async def test_delivery_failure_requires_reason_for_internal_authority():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="out_for_delivery", payment_status="paid", total=10)
    session = _mock_session(order)
    with pytest.raises(AppError) as exc:
        await service.update_status(session, 1, "delivery_failed", note="   ", tenant_slug="acme")
    assert exc.value.code == "DELIVERY_FAILURE_REASON_REQUIRED"
    session.commit.assert_not_called()


async def test_external_authority_delivery_failure_needs_no_reason():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="out_for_delivery", payment_status="paid", total=10)
    session = _mock_session(order)
    result = await service.update_status(
        session,
        1,
        "delivery_failed",
        tenant_slug="acme",
        authority=service.TransitionAuthority.EXTERNAL,
    )
    assert result.status == "delivery_failed"


async def test_delivery_failure_notifies_customer_and_staff():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=7, user_id=5, status="out_for_delivery", payment_status="paid", total=10)
    session = _mock_session(order)
    with patch("app.modules.orders.service.notify_user", new_callable=AsyncMock) as notify_user, patch(
        "app.modules.orders.service.notify_staff", new_callable=AsyncMock
    ) as notify_staff:
        await service.update_status(
            session, 7, "delivery_failed", note="Client absent", tenant_slug="acme"
        )

    assert notify_user.await_args.kwargs["event"] == "order.delivery_failed"
    assert notify_user.await_args.kwargs["user_id"] == 5
    assert notify_staff.await_args.kwargs["event"] == "order.delivery_failed"
    history = session.add.call_args_list[0].args[0]
    assert history.status == "delivery_failed" and history.note == "Client absent"


async def test_update_status_locks_the_order_row():
    from app.modules.orders import service
    from app.modules.orders.models import Order

    order = Order(id=1, status="ready", payment_status="paid", total=10)
    session = _mock_session(order)
    await service.update_status(session, 1, "out_for_delivery", tenant_slug="acme")

    kwargs = session.get.await_args.kwargs
    assert kwargs["with_for_update"] is True
    assert kwargs["populate_existing"] is True


async def test_concurrent_status_updates_are_serialized(db_engine, bootstrap_default_tenant):
    """Deux validations simultanees de la meme commande : une seule passe.

    Vraies connexions et vrais COMMIT (le verrou de ligne n'a de sens qu'entre
    transactions distinctes) ; la commande de test est supprimee a la fin.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.database import tenant_schema_name
    from app.modules.orders import service
    from app.modules.orders.models import Order, OrderStatusHistory

    schema = tenant_schema_name(bootstrap_default_tenant["tenant_slug"])

    async def open_session() -> AsyncSession:
        session = AsyncSession(bind=await db_engine.connect(), expire_on_commit=False)
        await session.execute(sa.text(f'SET search_path TO "{schema}", public'))
        return session

    setup = await open_session()
    order = Order(status="ready", payment_status="paid", order_type="delivery", total=10)
    setup.add(order)
    await setup.commit()
    order_id = order.id
    await setup.close()

    async def attempt(session: AsyncSession):
        # Elargit la fenetre de course : sans verrou de ligne, les deux transactions lisent
        # "ready" avant que la premiere ne commit, et les deux "reussissent".
        real_commit = session.commit

        async def slow_commit():
            await asyncio.sleep(0.4)
            await real_commit()

        session.commit = slow_commit
        try:
            await service.update_status(session, order_id, "out_for_delivery", tenant_slug="test")
            return "ok"
        except AppError as exc:
            await session.rollback()
            return exc.code
        finally:
            await session.close()

    try:
        sessions = [await open_session(), await open_session()]
        results = await asyncio.gather(*(attempt(s) for s in sessions))
        assert sorted(results) == ["INVALID_STATUS_TRANSITION", "ok"]

        check = await open_session()
        history_count = await check.scalar(
            sa.select(sa.func.count())
            .select_from(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == order_id, OrderStatusHistory.status == "out_for_delivery")
        )
        await check.close()
        assert history_count == 1
    finally:
        cleanup = await open_session()
        await cleanup.execute(sa.text("DELETE FROM orders WHERE id = :id"), {"id": order_id})
        await cleanup.commit()
        await cleanup.close()


# --- Remboursement et zones -------------------------------------------------


async def test_refund_guard_accepts_delivery_failed_but_not_in_flight_orders(db_session):
    from app.modules.orders.models import Order
    from app.modules.payments.service import create_refund

    delivered_failed = Order(status="delivery_failed", payment_status="paid", total=10)
    in_flight = Order(status="out_for_delivery", payment_status="paid", total=10)
    db_session.add_all([delivered_failed, in_flight])
    await db_session.flush()

    with pytest.raises(AppError) as blocked:
        await create_refund(db_session, "test", in_flight.id, None, None, "test")
    assert blocked.value.code == "REFUND_NOT_ALLOWED"

    # Passe le garde-fou de statut : echoue ensuite faute de paiement, pas de statut.
    with pytest.raises(AppError) as allowed:
        await create_refund(db_session, "test", delivered_failed.id, None, None, "Client absent")
    assert allowed.value.code == "PAYMENT_NOT_FOUND"


async def test_update_unknown_zone_returns_404_not_500():
    from app.modules.delivery import router
    from app.modules.delivery.schemas import DeliveryZoneCreate

    body = DeliveryZoneCreate(name="Fantome", polygon=_SQUARE_A, fee=1)
    with pytest.raises(AppError) as exc:
        await router.update_zone(999999, body, current_user={"tenant_slug": "test"})
    assert exc.value.code == "DELIVERY_ZONE_NOT_FOUND"
    assert exc.value.status_code == 404
