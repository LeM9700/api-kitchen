"""Tests pour worker/tasks/order_hub.py et app/modules/orders/hub_status.py.

Suit le pattern de tests/test_worker_catalog_sync.py : monkeypatch de
create_async_engine/async_sessionmaker pour rediriger toutes les sessions
ouvertes par la tache vers db_session (isole par savepoint/rollback), et
AsyncMock pour l'engine espion (assertion sur dispose()).
"""
from unittest.mock import AsyncMock

import sqlalchemy as sa
import pytest

from app.modules.orders.hub_status import apply_hub_status
from app.modules.orders.models import Order, OrderHubTransmission, ProcessedHubOrderEvent
from app.modules.orders.ports import HubPushResult
from app.modules.orders.service import TransitionAuthority


def _patch_engine_and_sessions(monkeypatch, module, db_session):
    fake_engine = AsyncMock()
    monkeypatch.setattr(module, "create_async_engine", lambda *a, **kw: fake_engine)
    monkeypatch.setattr(module, "async_sessionmaker", lambda *a, **kw: (lambda: db_session))
    return fake_engine


async def _seed_order(db_session, idempotency_key: str = "idem-1") -> Order:
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    order = Order(idempotency_key=idempotency_key, total=12.5, status="pending")
    db_session.add(order)
    await db_session.flush()
    return order


def _fake_connection() -> dict:
    return {"id": 1, "access_token_encrypted": "cipher"}


# --- push_order_to_hub -------------------------------------------------------


async def test_push_order_to_hub_sends_and_records_transmission(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-push-1")
    await db_session.commit()

    fake_engine = _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.hub_client, "is_configured", lambda: True)
    monkeypatch.setattr(order_hub.pos_service, "get_active_connection", AsyncMock(return_value=_fake_connection()))
    monkeypatch.setattr(order_hub.hub_client, "decrypt_access_token", lambda v: "plaintext-token")

    push_mock = AsyncMock(return_value=HubPushResult(hub_order_id="hub-42"))
    monkeypatch.setattr(order_hub.hub_client.HttpHubOrderClient, "push_order", push_mock)

    await order_hub.push_order_to_hub({"redis": AsyncMock()}, order_id=order.id, tenant_slug="pizza_test")

    # [SECURITE] private_reference reutilise l'Idempotency-Key -- aucune nouvelle cle generee.
    push_mock.assert_awaited_once()
    called_order, private_reference, access_token = push_mock.call_args.args
    assert private_reference == "idem-push-1"
    assert access_token == "plaintext-token"

    transmission = await db_session.scalar(
        sa.select(OrderHubTransmission).where(OrderHubTransmission.order_id == order.id)
    )
    assert transmission.transmission_status == "sent"
    assert transmission.hub_order_id == "hub-42"
    assert transmission.sent_at is not None
    fake_engine.dispose.assert_awaited_once()


async def test_push_order_to_hub_noop_when_hub_not_configured(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-push-2")
    await db_session.commit()

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.hub_client, "is_configured", lambda: False)
    get_connection_mock = AsyncMock()
    monkeypatch.setattr(order_hub.pos_service, "get_active_connection", get_connection_mock)

    await order_hub.push_order_to_hub({"redis": AsyncMock()}, order_id=order.id, tenant_slug="pizza_test")

    get_connection_mock.assert_not_awaited()


async def test_push_order_to_hub_noop_when_already_sent(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-push-3")
    db_session.add(OrderHubTransmission(order_id=order.id, transmission_status="sent"))
    await db_session.commit()

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.hub_client, "is_configured", lambda: True)
    monkeypatch.setattr(order_hub.pos_service, "get_active_connection", AsyncMock(return_value=_fake_connection()))
    push_mock = AsyncMock()
    monkeypatch.setattr(order_hub.hub_client.HttpHubOrderClient, "push_order", push_mock)

    await order_hub.push_order_to_hub({"redis": AsyncMock()}, order_id=order.id, tenant_slug="pizza_test")

    push_mock.assert_not_awaited()


async def test_push_order_to_hub_marks_failed_on_error_and_reraises(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-push-4")
    await db_session.commit()

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.hub_client, "is_configured", lambda: True)
    monkeypatch.setattr(order_hub.pos_service, "get_active_connection", AsyncMock(return_value=_fake_connection()))
    monkeypatch.setattr(order_hub.hub_client, "decrypt_access_token", lambda v: "plaintext-token")
    monkeypatch.setattr(
        order_hub.hub_client.HttpHubOrderClient,
        "push_order",
        AsyncMock(side_effect=ConnectionError("boom")),
    )

    with pytest.raises(RuntimeError):
        # job_try=1 < max_tries -- with_dead_letter re-leve sans ecrire en dead-letter.
        await order_hub.push_order_to_hub({"job_try": 1, "redis": AsyncMock()}, order_id=order.id, tenant_slug="pizza_test")

    transmission = await db_session.scalar(
        sa.select(OrderHubTransmission).where(OrderHubTransmission.order_id == order.id)
    )
    assert transmission.transmission_status == "failed"
    assert transmission.last_error == "ConnectionError"


# --- process_hub_order_callback ----------------------------------------------


def _callback_body(**overrides) -> str:
    import json

    payload = {
        "event_id": "evt-1",
        "external_establishment_id": "est-1",
        "status": "accepted",
        "private_reference": "idem-cb-1",
    }
    payload.update(overrides)
    return json.dumps(payload)


async def test_process_hub_order_callback_applies_valid_transition(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-cb-1")
    order.payment_status = "paid"
    db_session.add(OrderHubTransmission(order_id=order.id))
    await db_session.commit()
    order_id = order.id

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(
        order_hub.webhook_service,
        "resolve_order_context",
        AsyncMock(return_value={"connection_id": 1, "tenant_slug": "pizza_test"}),
    )

    await order_hub.process_hub_order_callback({"redis": AsyncMock()}, raw_body=_callback_body())

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    refreshed_order = await db_session.get(Order, order_id)
    assert refreshed_order.status == "confirmed"

    transmission = await db_session.scalar(
        sa.select(OrderHubTransmission).where(OrderHubTransmission.order_id == order_id)
    )
    assert transmission.last_hub_status == "accepted"
    assert transmission.transmission_status == "acknowledged"

    history = await db_session.scalar(
        sa.text(
            "SELECT authority FROM order_status_history WHERE order_id = :order_id ORDER BY id DESC LIMIT 1"
        ),
        {"order_id": order.id},
    )
    assert history == TransitionAuthority.EXTERNAL.value


async def test_process_hub_order_callback_duplicate_event_ignored(db_session, monkeypatch):
    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-cb-2")
    db_session.add(OrderHubTransmission(order_id=order.id))
    db_session.add(ProcessedHubOrderEvent(event_id="evt-dup"))
    await db_session.commit()
    order_id = order.id

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(
        order_hub.webhook_service,
        "resolve_order_context",
        AsyncMock(return_value={"connection_id": 1, "tenant_slug": "pizza_test"}),
    )

    await order_hub.process_hub_order_callback(
        {"redis": AsyncMock()},
        raw_body=_callback_body(event_id="evt-dup", private_reference="idem-cb-2"),
    )

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    refreshed_order = await db_session.get(Order, order_id)
    assert refreshed_order.status == "pending"


async def test_process_hub_order_callback_unknown_establishment_ignored(db_session, monkeypatch):
    from worker.tasks import order_hub

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.webhook_service, "resolve_order_context", AsyncMock(return_value=None))

    # Ne doit jamais lever, meme si l'etablissement est inconnu.
    await order_hub.process_hub_order_callback({"redis": AsyncMock()}, raw_body=_callback_body())


async def test_process_hub_order_callback_invalid_payload_ignored(db_session, monkeypatch):
    from worker.tasks import order_hub

    resolve_mock = AsyncMock()
    monkeypatch.setattr(order_hub.webhook_service, "resolve_order_context", resolve_mock)

    await order_hub.process_hub_order_callback({"redis": AsyncMock()}, raw_body="not json")

    resolve_mock.assert_not_awaited()


# --- apply_hub_status (logique partagee callback/reconciliation) -----------


async def test_apply_hub_status_ignores_stale_status(db_session, monkeypatch):
    order = await _seed_order(db_session, idempotency_key="idem-stale-1")
    db_session.add(OrderHubTransmission(order_id=order.id, last_hub_status="preparing"))
    await db_session.commit()

    # "received" est de rang inferieur a "preparing" deja connu -- ignore.
    await apply_hub_status(db_session, order.id, "received", tenant_slug="pizza_test")

    await db_session.refresh(order)
    assert order.status == "pending"
    transmission = await db_session.scalar(
        sa.select(OrderHubTransmission).where(OrderHubTransmission.order_id == order.id)
    )
    assert transmission.last_hub_status == "preparing"


async def test_apply_hub_status_intermediate_status_updates_transmission_only(db_session):
    order = await _seed_order(db_session, idempotency_key="idem-intermediate-1")
    db_session.add(OrderHubTransmission(order_id=order.id))
    await db_session.commit()

    await apply_hub_status(db_session, order.id, "preparing", tenant_slug="pizza_test")

    await db_session.refresh(order)
    assert order.status == "pending"
    transmission = await db_session.scalar(
        sa.select(OrderHubTransmission).where(OrderHubTransmission.order_id == order.id)
    )
    assert transmission.last_hub_status == "preparing"


# --- reconcile_hub_orders -----------------------------------------------------


async def test_reconcile_hub_orders_alerts_once_when_never_acknowledged(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from worker.tasks import order_hub

    order = await _seed_order(db_session, idempotency_key="idem-reconcile-1")
    transmission = OrderHubTransmission(
        order_id=order.id,
        transmission_status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(minutes=60),
    )
    db_session.add(transmission)
    await db_session.commit()

    _patch_engine_and_sessions(monkeypatch, order_hub, db_session)
    monkeypatch.setattr(order_hub.hub_client, "is_status_configured", lambda: True)
    monkeypatch.setattr(order_hub, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(order_hub, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(order_hub, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(order_hub.pos_service, "get_active_connection", AsyncMock(return_value=_fake_connection()))
    monkeypatch.setattr(order_hub.hub_client, "decrypt_access_token", lambda v: "plaintext-token")
    monkeypatch.setattr(order_hub.hub_client.HttpHubOrderClient, "fetch_status", AsyncMock(return_value=None))
    notify_mock = AsyncMock()
    monkeypatch.setattr(order_hub, "notify_staff", notify_mock)

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    class _FakePublicSession:
        async def execute(self, *a, **kw):
            return _FakeResult([("pizza_test",)])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(order_hub, "get_public_session", lambda: _FakePublicSession())

    transmission_id = transmission.id

    await order_hub.reconcile_hub_orders({"redis": AsyncMock()})

    notify_mock.assert_awaited_once()
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    refreshed_transmission = await db_session.get(OrderHubTransmission, transmission_id)
    assert refreshed_transmission.alerted_at is not None

    # Deuxieme run : alerted_at deja renseigne -- pas de deuxieme alerte.
    notify_mock.reset_mock()
    await order_hub.reconcile_hub_orders({"redis": AsyncMock()})
    notify_mock.assert_not_awaited()
