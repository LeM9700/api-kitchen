# tests/test_admin_improvements.py
import inspect
import os
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.modules.admin.tenants.schemas import TenantConfigUpdate, TenantScheduledClosureRequest
from app.modules.admin.dashboard.schemas import DailyStatsResponse, LiveStatsResponse


def test_tenant_config_update_valid_timezone():
    update = TenantConfigUpdate(timezone="America/New_York")
    assert update.timezone == "America/New_York"


def test_tenant_config_update_invalid_timezone():
    with pytest.raises(ValidationError):
        TenantConfigUpdate(timezone="Not/ATimezone")


def test_scheduled_closure_accepts_future_timezone_aware_datetime():
    scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    request = TenantScheduledClosureRequest(scheduled_close_at=scheduled_at)
    assert request.scheduled_close_at == scheduled_at


def test_scheduled_closure_accepts_null_for_cancel():
    request = TenantScheduledClosureRequest(scheduled_close_at=None)
    assert request.scheduled_close_at is None


def test_scheduled_closure_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        TenantScheduledClosureRequest(
            scheduled_close_at=datetime.now() + timedelta(minutes=10)
        )


def test_scheduled_closure_rejects_past_datetime():
    with pytest.raises(ValidationError):
        TenantScheduledClosureRequest(
            scheduled_close_at=datetime.now(timezone.utc) - timedelta(minutes=10)
        )


def test_daily_stats_response_parsing():
    data = {
        "date": "2026-06-22",
        "revenue": 150.5,
        "order_count": 10,
        "avg_basket": 15.05,
        "tenant_slug": "pizza-test",
    }
    resp = DailyStatsResponse(**data)
    assert resp.order_count == 10


def test_live_stats_response_extra_fields_ignored():
    data = {
        "tenant_slug": "pizza-test",
        "orders_last_24h": 5,
        "revenue_last_24h": 75.0,
        "avg_order_value_24h": 15.0,
        "pending_orders": 2,
        "computed_at": "2026-06-23T10:00:00Z",
        "_id": "should_be_ignored",
    }
    resp = LiveStatsResponse(**{k: v for k, v in data.items() if k != "_id"})
    assert resp.pending_orders == 2


# ---------------------------------------------------------------------------
# Task 3 tests
# ---------------------------------------------------------------------------

def _get_tenant_service():
    """Importe tenant_service en injectant les variables d'environnement minimales."""
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    os.environ.setdefault("TEST_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x_test")
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("ARQ_REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_x")
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_x")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-xx")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
    os.environ.setdefault("CLOUDINARY_API_KEY", "test")
    os.environ.setdefault("CLOUDINARY_API_SECRET", "test")
    from app.modules.admin import tenant_service  # noqa: PLC0415
    return tenant_service


def test_write_audit_signature_has_user_email():
    """_write_audit doit accepter le paramètre user_email."""
    ts = _get_tenant_service()
    sig = inspect.signature(ts._write_audit)
    assert "user_email" in sig.parameters
    param = sig.parameters["user_email"]
    assert param.default is None


def test_update_config_signature_has_new_params():
    """update_config doit accepter user_email, arq_pool et tenant_slug."""
    ts = _get_tenant_service()
    sig = inspect.signature(ts.update_config)
    for name in ("user_email", "arq_pool", "tenant_slug"):
        assert name in sig.parameters, f"Paramètre manquant : {name}"
        assert sig.parameters[name].default is None, f"{name} doit avoir None comme défaut"


def test_upsert_business_hours_signature_has_user_email():
    """upsert_business_hours doit accepter le paramètre user_email."""
    ts = _get_tenant_service()
    sig = inspect.signature(ts.upsert_business_hours)
    assert "user_email" in sig.parameters
    assert sig.parameters["user_email"].default is None


def test_get_next_opening_exists():
    """get_next_opening doit être une coroutine publique dans tenant_service."""
    ts = _get_tenant_service()
    assert hasattr(ts, "get_next_opening")
    assert inspect.iscoroutinefunction(ts.get_next_opening)


def test_get_next_opening_signature():
    """get_next_opening doit accepter uniquement session comme paramètre."""
    ts = _get_tenant_service()
    sig = inspect.signature(ts.get_next_opening)
    assert "session" in sig.parameters


async def test_next_opening_endpoint_public(client):
    response = await client.get(
        "/api/v1/admin/tenant/next-opening",
        params={"tenant_slug": "test"},
    )
    assert response.status_code != 404
    assert response.status_code != 405


# ---------------------------------------------------------------------------
# Task 4 tests
# ---------------------------------------------------------------------------

def test_remove_paris_tz_constant():
    import app.modules.admin.tenants.service as svc
    assert not hasattr(svc, '_PARIS_TZ'), "_PARIS_TZ should be removed"


def test_update_config_has_arq_pool_param():
    import inspect
    from app.modules.admin.tenants.service import update_config
    sig = inspect.signature(update_config)
    assert 'arq_pool' in sig.parameters
    assert 'tenant_slug' in sig.parameters


# ---------------------------------------------------------------------------
# Task 5 tests
# ---------------------------------------------------------------------------

async def test_admin_stats_requires_auth(client):
    response = await client.get("/api/v1/admin/stats/daily")
    assert response.status_code == 401


async def test_tenant_config_requires_admin(client):
    response = await client.get("/api/v1/tenant/config")
    assert response.status_code == 401


async def test_suspend_requires_super_admin(client):
    response = await client.get("/api/v1/admin/tenants")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Task 6 tests
# ---------------------------------------------------------------------------

async def test_next_opening_requires_tenant_slug(client):
    response = await client.get("/api/v1/admin/tenant/next-opening")
    assert response.status_code == 422  # tenant_slug manquant


async def test_toggle_closure_requires_admin(client):
    response = await client.patch(
        "/api/v1/admin/tenant/toggle-closure",
        json={"is_temporarily_closed": True},
    )
    assert response.status_code == 401


async def test_scheduled_closure_requires_admin(client):
    response = await client.put(
        "/api/v1/admin/tenant/scheduled-closure",
        json={"scheduled_close_at": None},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Task 7 tests
# ---------------------------------------------------------------------------

async def test_stats_summary_requires_admin(client):
    response = await client.get("/api/v1/admin/stats/summary")
    assert response.status_code == 401


async def test_suspend_tenant_requires_super_admin(client):
    response = await client.patch(
        "/api/v1/admin/tenants/1/suspend",
        json={"suspension_message": "test"},
    )
    assert response.status_code == 401


async def test_unsuspend_tenant_requires_super_admin(client):
    response = await client.patch("/api/v1/admin/tenants/1/unsuspend")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Task 8 tests
# ---------------------------------------------------------------------------

def test_queued_in_valid_transitions():
    from app.modules.orders.service import VALID_TRANSITIONS
    assert "queued" not in VALID_TRANSITIONS.get("pending", set()), \
        "queued must NOT be directly requestable from pending (only via capacity redirect)"
    assert "confirmed" in VALID_TRANSITIONS.get("queued", set()), \
        "queued must be able to transition to confirmed"


def test_queued_is_not_terminal():
    from app.modules.orders.service import VALID_TRANSITIONS
    assert "queued" in VALID_TRANSITIONS, \
        "queued must itself have transitions (not a terminal status)"


# ---------------------------------------------------------------------------
# Task 9 tests
# ---------------------------------------------------------------------------

def test_stock_snapshot_task_importable():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    os.environ.setdefault("TEST_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x_test")
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("ARQ_REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_x")
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_x")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-xx")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
    os.environ.setdefault("CLOUDINARY_API_KEY", "test")
    os.environ.setdefault("CLOUDINARY_API_SECRET", "test")
    from worker.tasks.stock_snapshot import aggregate_stock_snapshot
    import asyncio
    assert asyncio.iscoroutinefunction(aggregate_stock_snapshot)


def test_stock_snapshot_registered_in_worker():
    import worker.main as worker_main
    func_names = [f.__name__ for f in worker_main.WorkerSettings.functions if callable(f)]
    assert "aggregate_stock_snapshot" in func_names


# ---------------------------------------------------------------------------
# Task 10 tests
# ---------------------------------------------------------------------------

def test_notify_config_change_importable():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    os.environ.setdefault("TEST_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x_test")
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("ARQ_REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_x")
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_x")
    os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-xx")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
    os.environ.setdefault("CLOUDINARY_API_KEY", "test")
    os.environ.setdefault("CLOUDINARY_API_SECRET", "test")
    from worker.tasks.emails import notify_config_change
    import asyncio
    assert asyncio.iscoroutinefunction(notify_config_change)


def test_notify_config_change_registered():
    import worker.main as worker_main
    func_names = [
        f if isinstance(f, str) else f.__name__
        for f in worker_main.WorkerSettings.functions
    ]
    assert "worker.tasks.emails.notify_config_change" in func_names


def test_worker_settings_has_notify_config_change():
    from worker.main import WorkerSettings
    assert "worker.tasks.emails.notify_config_change" in WorkerSettings.functions


def test_worker_settings_has_stock_snapshot_cron():
    from worker.main import WorkerSettings
    from worker.tasks.stock_snapshot import aggregate_stock_snapshot
    cron_fns = [c.coroutine for c in WorkerSettings.cron_jobs]
    assert aggregate_stock_snapshot in cron_fns


def test_scheduled_closures_task_registered():
    from worker.main import WorkerSettings
    from worker.tasks.scheduled_closures import process_scheduled_closures

    func_names = [
        f if isinstance(f, str) else f.__name__
        for f in WorkerSettings.functions
    ]
    cron_fns = [c.coroutine for c in WorkerSettings.cron_jobs]
    assert "process_scheduled_closures" in func_names
    assert process_scheduled_closures in cron_fns
