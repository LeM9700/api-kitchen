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
    from app.modules.catalog import snapshot_repository
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90001)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
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
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90002)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=False))
    fetch_mock = AsyncMock()
    monkeypatch.setattr(catalog_sync.HttpHubCatalogClient, "fetch_catalog", fetch_mock)

    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=90002)

    fetch_mock.assert_not_awaited()
    fake_engine.dispose.assert_awaited_once()


async def test_sync_catalog_from_hub_re_enqueues_when_rate_limited(db_session, monkeypatch):
    from unittest.mock import AsyncMock as Mock

    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90003)

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
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

    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90004)

    release_mock = AsyncMock()
    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
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

    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError
    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90005)

    release_mock = AsyncMock()
    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)
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

    from worker.tasks import catalog_sync

    await _seed_active_connection(db_session, connection_id=90104)
    await _seed_active_connection(db_session, connection_id=90105)
    await _seed_active_connection_with_broken_tenant_slug(db_session, connection_id=90106)
    # 90104 and 90105 have no snapshot -- both must still be enqueued despite 90106 failing.

    fake_engine = _patch_engine_and_sessions(monkeypatch, db_session)

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


def test_sync_catalog_from_hub_registered_in_worker_settings():
    from worker.main import WorkerSettings

    names = [f if isinstance(f, str) else f.__name__ for f in WorkerSettings.functions]
    assert "worker.tasks.catalog_sync.sync_catalog_from_hub" in names or "sync_catalog_from_hub" in names


def test_sync_stale_catalog_connections_registered_as_cron():
    from worker.main import WorkerSettings

    cron_functions = [job.coroutine.__name__ for job in WorkerSettings.cron_jobs]
    assert "sync_stale_catalog_connections" in cron_functions
