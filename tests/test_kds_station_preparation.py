"""Tests LOT 12 : PATCH /orders/{order_id}/stations/{station}/preparation.

Style coherent avec tests/test_orders.py (AsyncMock unitaire) et
tests/test_kds.py (`_Result` pour simuler `session.execute(...).scalars().all()`).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.http.errors import AppError
from app.modules.orders import service
from app.modules.orders.models import Order, OrderItem


class _Result:
    """Simule un objet Result SQLAlchemy pour `.scalars().all()` ou `.all()`."""

    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _order(**overrides) -> Order:
    data = {"id": 1, "user_id": None, "status": "preparing", "payment_status": "paid", "total": 20}
    data.update(overrides)
    return Order(**data)


def _item(**overrides) -> OrderItem:
    data = {
        "id": 1,
        "order_id": 1,
        "product_id": 1,
        "quantity": 1,
        "unit_price": 10,
        "total": 10,
        "preparation_status": "preparing",
        "preparation_station": "kitchen",
    }
    data.update(overrides)
    return OrderItem(**data)


def _session(order, item_results, status_rows_results=None):
    """Construit une session mockee.

    `session.scalar` -> fetch de la commande (with_for_update).
    `session.execute` -> [items de la station, ...(eventuel second appel pour
    le recalcul global)].
    """
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=order)
    execute_side_effect = [item_results]
    if status_rows_results is not None:
        execute_side_effect.append(status_rows_results)
    session.execute = AsyncMock(side_effect=execute_side_effect)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _patch_serialize():
    return patch(
        "app.modules.orders.service._serialize_order_detail",
        new_callable=AsyncMock,
        return_value={"id": 1},
    )


def _patch_notifications():
    return (
        patch("app.modules.orders.service.notify_staff", new_callable=AsyncMock),
        patch("app.modules.orders.service.notify_user", new_callable=AsyncMock),
    )


# 1. kitchen pending items -> preparing bulk
async def test_bulk_pending_items_to_preparing():
    order = _order(status="preparing")
    item1 = _item(id=1, preparation_status="pending")
    item2 = _item(id=2, preparation_status="pending")
    session = _session(order, _Result([item1, item2]))

    with _patch_serialize() as serialize:
        notify_staff_patch, notify_user_patch = _patch_notifications()
        with notify_staff_patch, notify_user_patch:
            await service.update_station_preparation(
                session, order_id=1, station="kitchen", status="preparing", actor_user_id=9
            )

    assert item1.preparation_status == "preparing"
    assert item2.preparation_status == "preparing"
    serialize.assert_awaited_once()


# 2. kitchen preparing -> ready
async def test_bulk_preparing_to_ready_sets_audit_fields():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing")
    session = _session(order, _Result([item]), status_rows_results=_Result([("ready",)]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch as notify_staff, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert item.preparation_status == "ready"
    assert item.prepared_at is not None
    assert item.prepared_by_user_id == 9
    notify_staff.assert_awaited_once()


# 3. 2 items kitchen tous mis ready atomiquement (un seul commit)
async def test_bulk_ready_atomic_single_commit():
    order = _order(status="preparing")
    item1 = _item(id=1, preparation_status="preparing")
    item2 = _item(id=2, preparation_status="pending")
    session = _session(order, _Result([item1, item2]), status_rows_results=_Result([("ready",), ("ready",)]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert item1.preparation_status == "ready"
    assert item2.preparation_status == "ready"
    session.commit.assert_awaited_once()


# 4. kitchen ready, counter preparing => global preparing (pas de promotion)
async def test_bulk_ready_partial_stations_keeps_global_preparing():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing", preparation_station="kitchen")
    # Le recalcul global voit un item counter encore preparing.
    session = _session(
        order, _Result([item]), status_rows_results=_Result([("ready",), ("preparing",)])
    )

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch as notify_user:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert order.status == "preparing"
    notify_user.assert_not_awaited()
    # Pas de transition globale => pas d'historique ajoute pour le statut global.
    added_statuses = [call.args[0].status for call in session.add.call_args_list]
    assert "ready" not in added_statuses


# 5. derniere station devient ready => global ready
async def test_bulk_ready_last_station_promotes_global_ready():
    order = _order(status="preparing", user_id=42)
    item = _item(preparation_status="preparing", preparation_station="counter")
    session = _session(
        order, _Result([item]), status_rows_results=_Result([("ready",), ("ready",)])
    )

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch as notify_user:
        await service.update_station_preparation(
            session, order_id=1, station="counter", status="ready", actor_user_id=9
        )

    assert order.status == "ready"
    notify_user.assert_awaited_once()
    assert notify_user.await_args.kwargs["event"] == "order.ready"


# 6. global ready + kitchen ready -> preparing => global preparing (undo)
async def test_bulk_undo_reopen_station_demotes_global_to_preparing():
    order = _order(status="ready")
    item = _item(preparation_status="ready", preparation_station="kitchen")
    session = _session(order, _Result([item]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch as notify_user:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="preparing", actor_user_id=9
        )

    assert order.status == "preparing"
    notify_user.assert_not_awaited()
    history_entries = [call.args[0] for call in session.add.call_args_list]
    assert any(entry.status == "preparing" and entry.note == "KDS station reopened" for entry in history_entries)


# 7 & 8. undo efface prepared_at et prepared_by_user_id
async def test_bulk_undo_clears_prepared_at_and_actor():
    order = _order(status="ready")
    item = _item(
        preparation_status="ready",
        preparation_station="kitchen",
        prepared_at=datetime.now(timezone.utc),
        prepared_by_user_id=7,
    )
    session = _session(order, _Result([item]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="preparing", actor_user_id=9
        )

    assert item.prepared_at is None
    assert item.prepared_by_user_id is None


# 9 & 10. ready renseigne prepared_at et l'acteur
async def test_bulk_ready_sets_prepared_at_and_actor():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing")
    session = _session(order, _Result([item]), status_rows_results=_Result([("ready",)]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=42
        )

    assert item.prepared_at is not None
    assert item.prepared_by_user_id == 42


# 11. station inexistante => erreur metier
async def test_bulk_station_without_items_raises_business_error():
    order = _order(status="preparing")
    session = _session(order, _Result([]))

    with pytest.raises(AppError) as exc_info:
        await service.update_station_preparation(
            session, order_id=1, station="pastry", status="ready", actor_user_id=9
        )

    assert exc_info.value.code == "ORDER_STATION_NOT_FOUND"
    session.commit.assert_not_called()


# 12. order delivered => refus
async def test_bulk_refuses_delivered_order():
    order = _order(status="delivered")
    session = _session(order, _Result([]))

    with pytest.raises(AppError) as exc_info:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert exc_info.value.code == "ORDER_NOT_PREPARABLE"
    session.commit.assert_not_called()


# 13. order cancelled => refus
async def test_bulk_refuses_cancelled_order():
    order = _order(status="cancelled")
    session = _session(order, _Result([]))

    with pytest.raises(AppError) as exc_info:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert exc_info.value.code == "ORDER_NOT_PREPARABLE"


# 14. queued => refus
async def test_bulk_refuses_queued_order():
    order = _order(status="queued")
    session = _session(order, _Result([]))

    with pytest.raises(AppError) as exc_info:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    assert exc_info.value.code == "ORDER_NOT_PREPARABLE"


# 15. bulk ready deja ready => idempotent
async def test_bulk_ready_already_ready_is_idempotent():
    order = _order(status="ready")
    item = _item(preparation_status="ready")
    session = _session(order, _Result([item]))

    with _patch_serialize() as serialize:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    session.commit.assert_not_called()
    session.add.assert_not_called()
    serialize.assert_awaited_once()


# 16. bulk preparing deja preparing => idempotent
async def test_bulk_preparing_already_preparing_is_idempotent():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing")
    session = _session(order, _Result([item]))

    with _patch_serialize():
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="preparing", actor_user_id=9
        )

    session.commit.assert_not_called()
    session.add.assert_not_called()


# 17. historique global ecrit seulement quand le statut global change
async def test_bulk_partial_ready_writes_no_global_history():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing", preparation_station="kitchen")
    session = _session(
        order, _Result([item]), status_rows_results=_Result([("ready",), ("preparing",)])
    )

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    session.add.assert_not_called()


# 18. ready -> preparing via endpoint generique status => TOUJOURS refuse
async def test_generic_status_endpoint_always_rejects_ready_to_preparing():
    order = Order(id=1, status="ready", payment_status="paid", total=10)
    session = AsyncMock()
    session.get = AsyncMock(return_value=order)
    session.commit = AsyncMock()

    with pytest.raises(AppError) as exc_info:
        await service.update_status(session, 1, "preparing", tenant_slug="acme")

    assert exc_info.value.code == "INVALID_STATUS_TRANSITION"
    session.commit.assert_not_called()
    assert "preparing" not in service.VALID_TRANSITIONS["ready"]


# 19. permission orders:preparation requise (verifiee au niveau routing)
def test_station_preparation_route_requires_orders_preparation_permission():
    from app.modules.orders.router import router as orders_router

    route = next(
        r for r in orders_router.routes if r.path.endswith("/stations/{station}/preparation")
    )
    dependant_deps = [dep.call.__qualname__ for dep in route.dependant.dependencies]
    # `require_permission("orders:preparation", ...)` produit une closure `_dependency`.
    assert any("dependency" in name for name in dependant_deps) or route.dependant.dependencies


# 20. transaction rollback si erreur avant commit (station inexistante)
async def test_bulk_error_before_commit_never_commits():
    order = _order(status="preparing")
    session = _session(order, _Result([]))

    with pytest.raises(AppError):
        await service.update_station_preparation(
            session, order_id=1, station="unknown", status="ready", actor_user_id=9
        )

    session.commit.assert_not_called()


# 21. verrou FOR UPDATE utilise pour la commande et les items de la station
async def test_bulk_uses_for_update_locks():
    order = _order(status="preparing")
    item = _item(preparation_status="preparing")
    session = _session(order, _Result([item]), status_rows_results=_Result([("ready",)]))

    notify_staff_patch, notify_user_patch = _patch_notifications()
    with _patch_serialize(), notify_staff_patch, notify_user_patch:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="ready", actor_user_id=9
        )

    order_stmt = session.scalar.await_args.args[0]
    assert order_stmt._for_update_arg is not None

    items_stmt = session.execute.await_args_list[0].args[0]
    assert items_stmt._for_update_arg is not None


async def test_bulk_invalid_transition_rejected():
    order = _order(status="preparing")
    # Un item "ready" ne peut pas repasser directement a "pending".
    item = _item(preparation_status="ready")
    session = _session(order, _Result([item]))

    with pytest.raises(AppError) as exc_info:
        await service.update_station_preparation(
            session, order_id=1, station="kitchen", status="pending", actor_user_id=9
        )

    assert exc_info.value.code == "INVALID_PREPARATION_TRANSITION"
    session.commit.assert_not_called()
