import sqlalchemy as sa
import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.orders.models import Order, OrderHubTransmission, ProcessedHubOrderEvent


async def test_order_hub_transmission_rejects_invalid_status(db_session):
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    order = Order()
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderHubTransmission(order_id=order.id, transmission_status="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_order_hub_transmission_enforces_one_per_order(db_session):
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    order = Order()
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderHubTransmission(order_id=order.id))
    await db_session.flush()

    db_session.add(OrderHubTransmission(order_id=order.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_processed_hub_order_event_enforces_unique_event_id(db_session):
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    db_session.add(ProcessedHubOrderEvent(event_id="evt-1"))
    await db_session.flush()

    db_session.add(ProcessedHubOrderEvent(event_id="evt-1"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
