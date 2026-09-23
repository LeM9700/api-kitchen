from datetime import datetime, timedelta, timezone


async def _reset_establishments(session):
    import sqlalchemy as sa
    from app.modules.hr.models import Establishment

    await session.execute(sa.text("UPDATE establishments SET is_active = false"))
    session.add_all(
        [
            Establishment(id=9001, name="Restaurant A", timezone="Europe/Paris", is_active=True),
            Establishment(id=9002, name="Restaurant B", timezone="Europe/Paris", is_active=True),
        ]
    )
    await session.flush()
    return 9001, 9002


async def _product(session):
    from app.modules.catalog.models import Product

    product = Product(name="Isolation pizza", base_price=12, is_active=True)
    session.add(product)
    await session.flush()
    return product


async def test_order_creation_requires_deterministic_active_establishment(db_session):
    import pytest
    import sqlalchemy as sa
    from app.core.http.errors import AppError
    from app.modules.orders import service as orders_service
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    est_a, _est_b = await _reset_establishments(db_session)
    product = await _product(db_session)

    explicit = await orders_service.create_order(
        db_session,
        OrderCreate(
            establishment_id=est_a,
            order_type="pickup",
            items=[OrderItemCreate(product_id=product.id, quantity=1)],
        ),
        user_id=1,
        idempotency_key="est-explicit",
    )
    assert explicit.establishment_id == est_a

    defaulted = await orders_service.create_order(
        db_session,
        OrderCreate(order_type="pickup", items=[OrderItemCreate(product_id=product.id, quantity=1)]),
        user_id=2,
        idempotency_key="est-default",
    )
    assert defaulted.establishment_id == est_a

    with pytest.raises(AppError) as missing:
        await orders_service.create_order(
            db_session,
            OrderCreate(
                establishment_id=999999,
                order_type="pickup",
                items=[OrderItemCreate(product_id=product.id, quantity=1)],
            ),
            user_id=3,
            idempotency_key="est-missing",
        )
    assert missing.value.code == "ESTABLISHMENT_NOT_FOUND"

    await db_session.execute(sa.text("UPDATE establishments SET is_active = false"))
    with pytest.raises(AppError) as no_active:
        await orders_service.create_order(
            db_session,
            OrderCreate(order_type="pickup", items=[OrderItemCreate(product_id=product.id, quantity=1)]),
            user_id=4,
            idempotency_key="est-none-active",
        )
    assert no_active.value.code == "ESTABLISHMENT_REQUIRED"


async def test_order_listing_is_scoped_by_establishment(db_session):
    from app.core.http.schemas import PaginationParams
    from app.modules.orders import service as orders_service
    from app.modules.orders.models import Order

    est_a, est_b = await _reset_establishments(db_session)
    db_session.add_all(
        [
            Order(establishment_id=est_a, status="pending", payment_status="paid", total=10),
            Order(establishment_id=est_b, status="pending", payment_status="paid", total=20),
        ]
    )
    await db_session.flush()

    items_a, total_a = await orders_service.list_orders(
        db_session,
        PaginationParams(page=1, page_size=20),
        statuses=["pending"],
        establishment_id=est_a,
    )
    items_b, total_b = await orders_service.list_orders(
        db_session,
        PaginationParams(page=1, page_size=20),
        statuses=["pending"],
        establishment_id=est_b,
    )

    assert total_a == 1
    assert {item["establishment_id"] for item in items_a} == {est_a}
    assert total_b == 1
    assert {item["establishment_id"] for item in items_b} == {est_b}


async def test_payment_establishment_is_derived_from_order(db_session):
    from app.core.http.schemas import PaginationParams
    from app.modules.orders.models import Order
    from app.modules.payments import service as payments_service
    from app.modules.payments.models import Payment

    est_a, est_b = await _reset_establishments(db_session)
    order_a = Order(establishment_id=est_a, status="delivered", payment_status="paid", total=10)
    order_b = Order(establishment_id=est_b, status="delivered", payment_status="paid", total=20)
    db_session.add_all([order_a, order_b])
    await db_session.flush()
    db_session.add_all(
        [
            Payment(order_id=order_a.id, provider="cash", amount=10, status="paid"),
            Payment(order_id=order_b.id, provider="cash", amount=20, status="paid"),
        ]
    )
    await db_session.flush()

    items_a, total_a = await payments_service.list_payments(
        db_session,
        PaginationParams(page=1, page_size=20),
        establishment_id=est_a,
    )
    summary_a = await payments_service.get_payment_summary(db_session, establishment_id=est_a)
    summary_b = await payments_service.get_payment_summary(db_session, establishment_id=est_b)

    assert total_a == 1
    assert items_a[0].order_id == order_a.id
    assert summary_a.collected_amount_cents == 1000
    assert summary_b.collected_amount_cents == 2000


async def test_group_overview_isolates_establishments_and_current_tenant(db_session):
    import sqlalchemy as sa
    from app.modules.admin.dashboard.router import build_group_overview
    from app.modules.hr.models import Establishment, EmployeeProfile, Shift, TimeClockEntry
    from app.modules.orders.models import Order
    from app.modules.payments.models import Payment

    est_a, est_b = await _reset_establishments(db_session)
    now = datetime.now(timezone.utc)
    order_a = Order(establishment_id=est_a, status="pending", payment_status="paid", total=15)
    order_b = Order(establishment_id=est_b, status="preparing", payment_status="paid", total=25)
    db_session.add_all([order_a, order_b])
    await db_session.flush()
    db_session.add_all(
        [
            Payment(order_id=order_a.id, provider="cash", amount=15, status="paid"),
            Payment(order_id=order_b.id, provider="cash", amount=25, status="paid"),
            EmployeeProfile(id=9101, user_id=9101, establishment_id=est_a),
            EmployeeProfile(id=9102, user_id=9102, establishment_id=est_b),
            Shift(
                employee_id=9101,
                establishment_id=est_a,
                starts_at=now - timedelta(hours=1),
                ends_at=now + timedelta(hours=2),
            ),
            Shift(
                employee_id=9102,
                establishment_id=est_b,
                starts_at=now - timedelta(hours=1),
                ends_at=now + timedelta(hours=2),
            ),
            TimeClockEntry(employee_id=9101, establishment_id=est_a, clock_in_at=now, method="web", status="open"),
        ]
    )
    await db_session.flush()

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    await db_session.execute(sa.text("UPDATE establishments SET is_active = false"))
    db_session.add(Establishment(id=9901, name="Restaurant C", timezone="Europe/Paris", is_active=True))
    await db_session.flush()

    await db_session.execute(sa.text('SET search_path TO "tenant_test", public'))
    overview = await build_group_overview(db_session)
    names = {item.establishment_name for item in overview.items}
    by_id = {item.establishment_id: item for item in overview.items}

    assert names == {"Restaurant A", "Restaurant B"}
    assert "Restaurant C" not in names
    assert by_id[est_a].active_orders == 1
    assert by_id[est_a].revenue_today == 15
    assert by_id[est_a].staff_present == 1
    assert by_id[est_b].active_orders == 1
    assert by_id[est_b].revenue_today == 25
    assert by_id[est_b].staff_present == 0


async def test_manual_order_and_historical_null_contract(db_session):
    import sqlalchemy as sa
    from app.modules.orders import service as orders_service
    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    est_a, _est_b = await _reset_establishments(db_session)
    product = await _product(db_session)
    result = await orders_service.create_manual_order(
        db_session,
        ManualOrderCreate(
            establishment_id=est_a,
            order_type="dine_in",
            table_number="12",
            items=[OrderItemCreate(product_id=product.id, quantity=1)],
            payment={"method": "cash", "amount_received": 20},
        ),
        actor_user_id=11,
        tenant_slug="test",
        idempotency_key="manual-est-a",
    )
    assert result["order"]["establishment_id"] == est_a

    nullable = (
        await db_session.execute(
            sa.text(
                """SELECT is_nullable FROM information_schema.columns
                   WHERE table_schema = 'tenant_test'
                   AND table_name = 'orders'
                   AND column_name = 'establishment_id'"""
            )
        )
    ).scalar_one()
    assert nullable == "YES"
