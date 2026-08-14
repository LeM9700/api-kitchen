import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.http.schemas import PaginationParams


async def _tenant_session():
    engine = create_async_engine(settings.test_database_url or settings.database_url)
    conn = await engine.connect()
    await conn.begin()
    await conn.execute(text('SET search_path TO "tenant_pizza_test", public'))
    session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    return engine, conn, session


async def _seed_product(session, **kwargs):
    from app.modules.catalog.models import Product

    defaults = {"name": "Regina", "base_price": 11.5, "is_active": True}
    defaults.update(kwargs)
    product = Product(**defaults)
    session.add(product)
    await session.flush()
    return product


async def _seed_snapshot(session, connection_id: int, synced_at=None):
    from datetime import datetime, timezone

    from app.modules.catalog import snapshot_repository

    snapshot = await snapshot_repository.upsert_snapshot(
        session, connection_id=connection_id, payload={}, normalized=[]
    )
    if synced_at is not None:
        snapshot.synced_at = synced_at
    else:
        snapshot.synced_at = datetime.now(timezone.utc)
    await session.commit()
    return snapshot


async def test_get_catalog_raises_when_connection_id_is_none():
    from app.modules.catalog.exceptions import CatalogSnapshotUnavailableError
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        provider = HubCatalogProvider(connection_id=None)
        with pytest.raises(CatalogSnapshotUnavailableError):
            await provider.get_catalog(session, PaginationParams(page=1, page_size=10))
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_raises_when_no_snapshot_exists():
    from app.modules.catalog.exceptions import CatalogSnapshotUnavailableError
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        provider = HubCatalogProvider(connection_id=123456)
        with pytest.raises(CatalogSnapshotUnavailableError):
            await provider.get_catalog(session, PaginationParams(page=1, page_size=10))
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_reads_from_products_table():
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        product = await _seed_product(session, name="Regina", base_price=11.5, tax_rate=0.1, external_product_id="ext-1")
        await _seed_snapshot(session, connection_id=555)

        provider = HubCatalogProvider(connection_id=555)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 1
        assert summaries[0].id == product.id
        assert summaries[0].name == "Regina"
        assert summaries[0].base_price == 11.5
        assert summaries[0].tax_rate == 0.1
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_ids_are_real_product_ids_not_hashes():
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        product = await _seed_product(session, external_product_id="ext-2")
        await _seed_snapshot(session, connection_id=556)

        provider = HubCatalogProvider(connection_id=556)
        summaries, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert summaries[0].id == product.id
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_merges_product_overrides():
    from app.modules.catalog import override_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import ProductOverrideCreate

    try:
        engine, conn, session = await _tenant_session()
        product = await _seed_product(
            session, image_url="https://hub.example.com/regina.jpg", external_product_id="ext-3"
        )
        await _seed_snapshot(session, connection_id=557)
        await override_repository.upsert_override(
            session, product.id, ProductOverrideCreate(image_url="https://cdn.mine.com/custom-regina.jpg", is_featured=True)
        )

        provider = HubCatalogProvider(connection_id=557)
        summaries, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert summaries[0].image_url == "https://cdn.mine.com/custom-regina.jpg"
        assert summaries[0].is_featured is True
        assert summaries[0].base_price == 11.5  # price is never affected by overrides
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_override_never_changes_price_or_tax_rate():
    """Garde-fou fiscal : meme si un override est present, prix et TVA servis
    restent exactement ceux de `products` -- ProductOverride n'expose aucune
    colonne prix/TVA (verrouille au niveau du provider)."""
    from app.modules.catalog import override_repository
    from app.modules.catalog.models import ProductOverride
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import ProductOverrideCreate

    try:
        engine, conn, session = await _tenant_session()
        product = await _seed_product(session, base_price=11.5, tax_rate=0.055, external_product_id="ext-4")
        await _seed_snapshot(session, connection_id=558)
        override = await override_repository.upsert_override(
            session, product.id, ProductOverrideCreate(description="Ma description maison", is_featured=True)
        )

        assert not hasattr(override, "price")
        assert not hasattr(override, "base_price")
        assert not hasattr(override, "tax_rate")
        assert isinstance(override, ProductOverride)

        provider = HubCatalogProvider(connection_id=558)
        summaries, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert summaries[0].base_price == 11.5
        assert summaries[0].tax_rate == 0.055
        assert summaries[0].description == "Ma description maison"
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_excludes_inactive_products():
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        await _seed_product(session, name="Anchois", external_product_id="ext-5a", is_active=True)
        await _seed_product(session, name="Bolognese", external_product_id="ext-5b", is_active=False)
        await _seed_snapshot(session, connection_id=559)

        provider = HubCatalogProvider(connection_id=559)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 1
        assert all(s.is_active for s in summaries)
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_paginates_via_local_provider():
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        for i in range(5):
            await _seed_product(session, name=f"Pizza {i}", external_product_id=f"ext-page-{i}")
        await _seed_snapshot(session, connection_id=560)

        provider = HubCatalogProvider(connection_id=560)
        page1, total1 = await provider.get_catalog(session, PaginationParams(page=1, page_size=2))
        page2, total2 = await provider.get_catalog(session, PaginationParams(page=2, page_size=2))

        assert total1 == 5 and total2 == 5
        assert len(page1) == 2
        assert len(page2) == 2
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_enqueues_resync_when_snapshot_is_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.core.config import settings as app_settings
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_staleness_minutes", 60)
        monkeypatch.setattr(app_settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
        await _seed_product(session, external_product_id="ext-6")
        await _seed_snapshot(session, connection_id=561, synced_at=datetime.now(timezone.utc) - timedelta(hours=2))

        redis = AsyncMock()
        provider = HubCatalogProvider(connection_id=561)
        summaries, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        assert len(summaries) == 1  # still served despite being stale
        redis.enqueue_job.assert_awaited_once_with("sync_catalog_from_hub", connection_id=561)
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_still_served_when_stale_enqueue_fails(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.core.config import settings as app_settings
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_staleness_minutes", 60)
        monkeypatch.setattr(app_settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
        await _seed_product(session, external_product_id="ext-7")
        await _seed_snapshot(session, connection_id=562, synced_at=datetime.now(timezone.utc) - timedelta(hours=2))

        redis = AsyncMock()
        redis.enqueue_job.side_effect = ConnectionError("redis down")
        provider = HubCatalogProvider(connection_id=562)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        assert total == 1
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_does_not_enqueue_resync_when_snapshot_is_fresh():
    from unittest.mock import AsyncMock

    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        await _seed_product(session, external_product_id="ext-8")
        await _seed_snapshot(session, connection_id=563)

        redis = AsyncMock()
        provider = HubCatalogProvider(connection_id=563)
        await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        redis.enqueue_job.assert_not_awaited()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_raises_when_snapshot_older_than_hard_expiry(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.core.config import settings as app_settings
    from app.modules.catalog.exceptions import CatalogSnapshotUnavailableError
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_hard_expiry_hours", 24)
        await _seed_product(session, external_product_id="ext-9")
        await _seed_snapshot(session, connection_id=564, synced_at=datetime.now(timezone.utc) - timedelta(hours=25))

        provider = HubCatalogProvider(connection_id=564)
        with pytest.raises(CatalogSnapshotUnavailableError):
            await provider.get_catalog(session, PaginationParams(page=1, page_size=10))
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_serves_snapshot_within_hard_expiry(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.core.config import settings as app_settings
    from app.modules.catalog.providers import HubCatalogProvider

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_hard_expiry_hours", 24)
        await _seed_product(session, external_product_id="ext-10")
        await _seed_snapshot(session, connection_id=565, synced_at=datetime.now(timezone.utc) - timedelta(hours=23))

        provider = HubCatalogProvider(connection_id=565)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 1
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_hub_catalog_provider_writes_raise_read_only():
    from app.modules.catalog.exceptions import ReadOnlyCatalogError
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import ProductCreate, ProductUpdate

    provider = HubCatalogProvider(connection_id=1)
    with pytest.raises(ReadOnlyCatalogError):
        await provider.create_product(None, ProductCreate(name="X", base_price=1.0), user_id=1)
    with pytest.raises(ReadOnlyCatalogError):
        await provider.update_product(None, 1, ProductUpdate(base_price=2.0), user_id=1)
    with pytest.raises(ReadOnlyCatalogError):
        await provider.delete_product(None, 1, user_id=1)


def test_hub_catalog_provider_satisfies_protocol():
    from app.modules.catalog.ports import CatalogProvider
    from app.modules.catalog.providers import HubCatalogProvider

    assert isinstance(HubCatalogProvider(connection_id=1), CatalogProvider)
