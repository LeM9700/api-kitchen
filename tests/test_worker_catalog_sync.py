from unittest.mock import AsyncMock

import pytest


async def _seed_active_connection(db_session, connection_id: int, tenant_slug: str = "pizza_test"):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO public'))
    tenant_id = await db_session.scalar(sa.text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": tenant_slug})
    await db_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(id, tenant_id, provider, external_establishment_id, access_token_encrypted, status, connected_at) "
            "VALUES (:id, :tenant_id, 'generic_hub', 'est-1', 'cipher', 'active', now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": connection_id, "tenant_id": tenant_id},
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
