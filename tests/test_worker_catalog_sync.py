from unittest.mock import AsyncMock

import pytest


async def _seed_active_connection(db_session, connection_id: int, tenant_slug: str = "pizza_test"):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO public'))
    tenant_id = await db_session.scalar(sa.text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": tenant_slug})
    # external_establishment_id is derived from connection_id (not a fixed "est-1")
    # because uq_pos_connections_provider_establishment (alembic 0044) is a *global*
    # unique constraint on (provider, external_establishment_id), not scoped per
    # tenant/connection -- a fixed value would collide as soon as a single test seeds
    # more than one active connection (as the stale-connections cron tests below do).
    await db_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(id, tenant_id, provider, external_establishment_id, access_token_encrypted, status, connected_at) "
            "VALUES (:id, :tenant_id, 'generic_hub', :external_establishment_id, 'cipher', 'active', now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": connection_id, "tenant_id": tenant_id, "external_establishment_id": f"est-{connection_id}"},
    )
    await db_session.commit()


def _patch_engine_and_sessions(monkeypatch, db_session):
    """Redirects the task's engine/session creation so every session it opens is the
    test's isolated, rollback-only ``db_session`` -- while still returning a spy engine
    whose ``dispose()`` can be asserted, proving the real engine-lifecycle wiring
    (``create_async_engine`` -> ... -> ``engine.dispose()``) is exercised end to end
    rather than bypassed.

    ``fake_engine`` stands in for the real ``AsyncEngine`` returned by
    ``create_async_engine`` inside ``sync_catalog_from_hub``; ``async_sessionmaker`` is
    patched to ignore that engine and always hand back ``db_session`` instead, so DB
    writes/reads made by the task stay inside the test's transaction/rollback boundary.
    """
    from worker.tasks import catalog_sync

    fake_engine = AsyncMock()
    monkeypatch.setattr(catalog_sync, "create_async_engine", lambda *a, **kw: fake_engine)
    monkeypatch.setattr(catalog_sync, "async_sessionmaker", lambda *a, **kw: (lambda: db_session))
    return fake_engine


async def test_sync_catalog_from_hub_upserts_snapshot(db_session, monkeypatch):
    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90001)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value={"products": [{"id": "ext-1", "name": "Regina", "price": 11.5}]}),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90001)

    import sqlalchemy as sa
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    snapshot = await snapshot_repository.get_snapshot(db_session, connection_id=90001)
    assert snapshot is not None
    assert snapshot.normalized[0]["external_id"] == "ext-1"

    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_skips_when_lock_not_acquired(db_session, monkeypatch):
    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90002)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=False))
    fetch_mock = AsyncMock()
    monkeypatch.setattr(catalog_sync.HttpHubCatalogClient, "fetch_catalog", fetch_mock)

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90002)

    fetch_mock.assert_not_awaited()
    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_re_enqueues_when_rate_limited(db_session, monkeypatch):
    from unittest.mock import AsyncMock as Mock

    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90003)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=False))
    fetch_mock = AsyncMock()
    monkeypatch.setattr(catalog_sync.HttpHubCatalogClient, "fetch_catalog", fetch_mock)

    redis = Mock()
    await catalog_sync.sync_catalog_from_hub({"redis": redis}, connection_id=90003)

    fetch_mock.assert_not_awaited()
    redis.enqueue_job.assert_awaited_once_with("sync_catalog_from_hub", connection_id=90003, _defer_by=30)
    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_noop_when_connection_not_found_or_inactive(monkeypatch, db_session):
    from worker.tasks import catalog_sync

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    fetch_mock = AsyncMock()
    monkeypatch.setattr(catalog_sync.HttpHubCatalogClient, "fetch_catalog", fetch_mock)

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=99999999)

    fetch_mock.assert_not_awaited()
    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_reraises_and_logs_type_only_on_hub_http_error(db_session, monkeypatch, caplog):
    """Proves fetch_catalog failures are caught, logged (exception TYPE only -- never the
    message, which could contain secrets/tokens/PII), re-raised (as a sanitized
    RuntimeError, not the original exception) for ARQ's retry, the sync lock is still
    released via the finally block, and the engine is still disposed."""
    import logging

    import httpx

    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90004)

    release_mock = AsyncMock()
    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", release_mock)
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))

    secret_message = "leaked bearer token abc123 in error body"
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(side_effect=httpx.HTTPError(secret_message)),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc_info:
            await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90004)

    # Not the raw httpx.HTTPError -- a sanitized RuntimeError carrying only the type name.
    assert not isinstance(exc_info.value, httpx.HTTPError)
    assert "HTTPError" in str(exc_info.value)
    assert secret_message not in str(exc_info.value)
    # `raise ... from None` must sever chaining (__cause__ = None, suppress_context =
    # True) so standard traceback formatting -- and with_dead_letter's str(error), which
    # only ever looks at the raised exception itself, never walks __context__ -- can't
    # recover the original message. (Note: __context__ itself is still set by Python's
    # implicit chaining regardless of `from None`; nothing in this codebase reads it.)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True

    release_mock.assert_awaited_once()
    fake_engine.dispose.assert_awaited_once()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTPError" in log_text
    assert secret_message not in log_text


async def test_sync_catalog_from_hub_reraises_and_logs_type_only_on_malformed_payload(db_session, monkeypatch, caplog):
    """Same guarantees as above, but for a payload that fetch_catalog returns successfully
    and normalize_catalog then rejects as malformed -- the raw payload must never appear
    in logs or in the re-raised exception's message either."""
    import logging

    from app.core.config import settings
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90005)

    release_mock = AsyncMock()
    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", release_mock)
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))

    secret_marker = "not-a-list-and-also-not-secret-but-must-not-leak"
    malformed_payload = {"products": secret_marker}
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value=malformed_payload),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc_info:
            await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90005)

    assert not isinstance(exc_info.value, MalformedHubCatalogPayloadError)
    assert "MalformedHubCatalogPayloadError" in str(exc_info.value)
    assert secret_marker not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True

    release_mock.assert_awaited_once()
    fake_engine.dispose.assert_awaited_once()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "MalformedHubCatalogPayloadError" in log_text
    assert secret_marker not in log_text


async def test_sync_stale_catalog_connections_enqueues_missing_and_stale_only(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.schemas import NormalizedCatalogProduct
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90101)
    await _seed_active_connection(db_session, connection_id=90102)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    fresh = await snapshot_repository.upsert_snapshot(
        db_session, connection_id=90101, payload={}, normalized=[NormalizedCatalogProduct(external_id="e", name="n", price=1.0)]
    )
    fresh.synced_at = datetime.now(timezone.utc)
    await db_session.commit()
    # 90102 has no snapshot at all -- must also be enqueued.

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")

    redis = AsyncMock()
    await catalog_sync.sync_stale_catalog_connections({"redis": redis})

    enqueued_ids = {call.kwargs["connection_id"] for call in redis.enqueue_job.await_args_list}
    assert 90101 not in enqueued_ids
    assert 90102 in enqueued_ids

    fake_engine.dispose.assert_awaited_once()


async def test_sync_stale_catalog_connections_enqueues_stale_snapshot(db_session, monkeypatch):
    """A connection whose snapshot exists but is older than the configured staleness
    window must also be enqueued -- not just connections with no snapshot at all."""
    from datetime import datetime, timedelta, timezone

    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.schemas import NormalizedCatalogProduct
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90103)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    stale = await snapshot_repository.upsert_snapshot(
        db_session, connection_id=90103, payload={}, normalized=[NormalizedCatalogProduct(external_id="e", name="n", price=1.0)]
    )
    stale.synced_at = datetime.now(timezone.utc) - timedelta(
        minutes=settings.pos_hub_snapshot_staleness_minutes + 1
    )
    await db_session.commit()

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")

    redis = AsyncMock()
    await catalog_sync.sync_stale_catalog_connections({"redis": redis})

    enqueued_ids = {call.kwargs["connection_id"] for call in redis.enqueue_job.await_args_list}
    assert 90103 in enqueued_ids

    fake_engine.dispose.assert_awaited_once()


async def _seed_active_connection_with_broken_tenant_slug(db_session, connection_id: int) -> None:
    """Seeds an active pos_connections row whose tenant has a slug that fails
    ``tenant_schema_name``'s validation (app/core/database/session.py) -- simulating a
    stale/corrupted ``public.tenants.slug`` row. Used to prove one connection's schema
    resolution failure doesn't stop sync_stale_catalog_connections from evaluating the
    rest. No DB constraint stops this: slug format is only validated at the app layer,
    not by a CHECK constraint on public.tenants.
    """
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO public'))
    broken_slug = f"Broken Slug!{connection_id}"
    tenant_id = await db_session.scalar(
        sa.text("INSERT INTO public.tenants (slug, name, plan) VALUES (:slug, :name, 'starter') RETURNING id"),
        {"slug": broken_slug, "name": "Broken Tenant"},
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(id, tenant_id, provider, external_establishment_id, access_token_encrypted, status, connected_at) "
            "VALUES (:id, :tenant_id, 'generic_hub', :external_establishment_id, 'cipher', 'active', now())"
        ),
        {"id": connection_id, "tenant_id": tenant_id, "external_establishment_id": f"est-{connection_id}"},
    )
    await db_session.commit()


async def test_sync_stale_catalog_connections_isolates_per_connection_failures(db_session, monkeypatch, caplog):
    """One connection whose tenant schema resolution raises (invalid/stale slug) must not
    stop the cron from evaluating and enqueuing the other active connections -- this cron
    is the hourly safety net for every active connection, not just the ones sorted before
    the first failure."""
    import logging

    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90104)
    await _seed_active_connection(db_session, connection_id=90105)
    await _seed_active_connection_with_broken_tenant_slug(db_session, connection_id=90106)
    # 90104 and 90105 have no snapshot -- both must still be enqueued despite 90106 failing.

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")

    redis = AsyncMock()
    with caplog.at_level(logging.INFO):
        await catalog_sync.sync_stale_catalog_connections({"redis": redis})

    enqueued_ids = {call.kwargs["connection_id"] for call in redis.enqueue_job.await_args_list}
    assert 90104 in enqueued_ids
    assert 90105 in enqueued_ids
    assert 90106 not in enqueued_ids

    fake_engine.dispose.assert_awaited_once()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "connection_id=90106" in log_text
    assert "AppError" in log_text
    assert "Broken Slug" not in log_text
    # Summary log proves the observability half of the fix: scanned/enqueued/failed counts.
    assert "sync_stale_catalog_connections: termine" in log_text
    assert "echecs=1" in log_text


async def test_sync_catalog_from_hub_skips_when_not_configured(db_session, monkeypatch):
    """Finding 1: pos_hub_catalog_url empty (the default, safe-looking state) must
    short-circuit before the lock/rate-limiter/hub are touched at all -- otherwise a
    tenant with an active connection but no hub URL configured gets retried (and
    dead-lettered) every single hour, forever."""
    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90006)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "")
    lock_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", lock_mock)
    fetch_mock = AsyncMock()
    monkeypatch.setattr(catalog_sync.HttpHubCatalogClient, "fetch_catalog", fetch_mock)

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90006)

    lock_mock.assert_not_awaited()
    fetch_mock.assert_not_awaited()
    fake_engine.dispose.assert_awaited_once()


async def test_sync_stale_catalog_connections_skips_when_not_configured(db_session, monkeypatch):
    """Finding 1: same global gate for the hourly cron -- no point scanning active
    connections at all when the hub isn't configured."""
    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90007)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "")

    redis = AsyncMock()
    await catalog_sync.sync_stale_catalog_connections({"redis": redis})

    redis.enqueue_job.assert_not_awaited()
    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_rejects_empty_catalog_overwriting_existing_snapshot(db_session, monkeypatch):
    """Finding 2: a hub response with an empty product list must never silently wipe
    out an already-populated snapshot (transient hub issue, wrong establishment id,
    partial response, application bug returning 200 with []) -- the restaurant's
    public menu must not disappear until a real update comes in."""
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.schemas import NormalizedCatalogProduct
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90008)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    original = await snapshot_repository.upsert_snapshot(
        db_session,
        connection_id=90008,
        payload={"products": [{"id": "ext-1", "name": "Regina", "price": 11.5}]},
        normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5)],
    )
    original_synced_at = original.synced_at
    original_normalized = original.normalized
    await db_session.commit()

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value={"products": []}),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90008)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    snapshot = await snapshot_repository.get_snapshot(db_session, connection_id=90008)
    assert snapshot is not None
    assert snapshot.normalized == original_normalized
    assert snapshot.synced_at == original_synced_at

    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_allows_empty_catalog_on_first_sync(db_session, monkeypatch):
    """Finding 2: when there is NO existing snapshot yet, an empty catalog from the
    hub is a legitimate (if unusual) first-sync state -- must be persisted as-is,
    not rejected."""
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90009)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value={"products": []}),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90009)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    snapshot = await snapshot_repository.get_snapshot(db_session, connection_id=90009)
    assert snapshot is not None
    assert snapshot.normalized == []

    fake_engine.dispose.assert_awaited_once()


def test_sync_catalog_from_hub_registered_in_worker_settings():
    from worker.main import WorkerSettings

    names = [f if isinstance(f, str) else f.__name__ for f in WorkerSettings.functions]
    assert "worker.tasks.catalog_sync.sync_catalog_from_hub" in names or "sync_catalog_from_hub" in names


def test_sync_stale_catalog_connections_registered_as_cron():
    from worker.main import WorkerSettings

    cron_functions = [job.coroutine.__name__ for job in WorkerSettings.cron_jobs]
    assert "sync_stale_catalog_connections" in cron_functions


async def test_sync_catalog_from_hub_inserts_new_product(db_session, monkeypatch):
    import sqlalchemy as sa

    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90201)

    _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(
            return_value={
                "products": [
                    {"id": "ext-new-1", "name": "Regina", "price": 11.5, "tax_rate": 0.1, "is_active": True}
                ]
            }
        ),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90201)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    row = (
        await db_session.execute(
            sa.text("SELECT name, base_price, tax_rate, is_active FROM products WHERE external_product_id = 'ext-new-1'")
        )
    ).mappings().first()
    assert row is not None
    assert row["name"] == "Regina"
    assert float(row["base_price"]) == 11.5
    assert float(row["tax_rate"]) == 0.1
    assert row["is_active"] is True


async def test_sync_catalog_from_hub_updates_existing_product(db_session, monkeypatch):
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog.models import Product
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90202)
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    existing = Product(name="Old Name", base_price=9.0, external_product_id="ext-upd-1")
    db_session.add(existing)
    await db_session.commit()
    existing_id = existing.id

    _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(
            return_value={"products": [{"id": "ext-upd-1", "name": "New Name", "price": 13.0, "tax_rate": 0.2}]}
        ),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90202)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    row = (
        await db_session.execute(sa.text("SELECT id, name, base_price, tax_rate FROM products WHERE id = :id"), {"id": existing_id})
    ).mappings().first()
    assert row["id"] == existing_id  # same row updated, not a duplicate insert
    assert row["name"] == "New Name"
    assert float(row["base_price"]) == 13.0
    assert float(row["tax_rate"]) == 0.2


async def test_sync_catalog_from_hub_deactivates_product_removed_from_hub(db_session, monkeypatch):
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog.models import Product
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90203)
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    gone = Product(name="Discontinued", base_price=8.0, external_product_id="ext-gone-1", is_active=True)
    db_session.add(gone)
    await db_session.commit()
    gone_id = gone.id

    _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    # ext-gone-1 is absent from this response -- it disappeared from the hub.
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value={"products": [{"id": "ext-still-here", "name": "Still Here", "price": 5.0}]}),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90203)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    row = (
        await db_session.execute(sa.text("SELECT id, is_active FROM products WHERE id = :id"), {"id": gone_id})
    ).mappings().first()
    assert row is not None  # never deleted
    assert row["is_active"] is False


async def test_sync_catalog_from_hub_empty_catalog_does_not_deactivate_existing_products(db_session, monkeypatch):
    """Finding #2 from the prior lot's final review, re-verified at the products
    level: an empty hub response must not wipe out a healthy materialized catalog."""
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.models import Product
    from app.modules.catalog.schemas import NormalizedCatalogProduct
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90204)
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    healthy = Product(name="Healthy", base_price=7.0, external_product_id="ext-healthy-1", is_active=True)
    db_session.add(healthy)
    await snapshot_repository.upsert_snapshot(
        db_session,
        connection_id=90204,
        payload={},
        normalized=[NormalizedCatalogProduct(external_id="ext-healthy-1", name="Healthy", price=7.0)],
    )
    healthy_id = healthy.id

    _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient, "fetch_catalog", AsyncMock(return_value={"products": []})
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90204)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    row = (
        await db_session.execute(sa.text("SELECT is_active FROM products WHERE id = :id"), {"id": healthy_id})
    ).mappings().first()
    assert row["is_active"] is True  # untouched -- the empty response was rejected before materialization ran


async def test_sync_catalog_from_hub_duplicate_external_id_in_same_batch_does_not_crash(db_session, monkeypatch):
    """Regression: a malformed hub payload repeating the same external_id twice
    used to create two in-memory Product rows for one not-yet-materialized
    external_id, tripping the uq_products_external_product_id constraint on
    flush. The second occurrence must reuse the row created by the first."""
    import sqlalchemy as sa

    from app.core.config import settings
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90205)

    _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(
            return_value={
                "products": [
                    {"id": "ext-dup-1", "name": "Regina", "price": 11.0},
                    {"id": "ext-dup-1", "name": "Regina (updated)", "price": 12.0},
                ]
            }
        ),
    )

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90205)

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    rows = (
        await db_session.execute(
            sa.text("SELECT name, base_price FROM products WHERE external_product_id = 'ext-dup-1'")
        )
    ).mappings().all()
    assert len(rows) == 1  # one row, not a constraint-violation crash or a duplicate
    assert rows[0]["name"] == "Regina (updated)"  # last item in the batch wins
    assert float(rows[0]["base_price"]) == 12.0
