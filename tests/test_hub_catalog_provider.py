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


async def test_get_catalog_serves_fresh_snapshot_without_network_call(monkeypatch):
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=555,
            payload={"raw": True},
            normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5, tax_rate=0.1)],
        )

        def _boom(*args, **kwargs):
            raise AssertionError("HubCatalogProvider.get_catalog must never call the hub over HTTP")

        monkeypatch.setattr("httpx.AsyncClient", _boom)

        provider = HubCatalogProvider(connection_id=555)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 1
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


async def test_get_catalog_merges_product_overrides():
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.models import ProductOverride
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=556,
            payload={},
            normalized=[
                NormalizedCatalogProduct(
                    external_id="ext-1", name="Regina", price=11.5, image_url="https://hub.example.com/regina.jpg"
                )
            ],
        )
        session.add(
            ProductOverride(
                connection_id=556,
                external_product_id="ext-1",
                image_url="https://cdn.mine.com/custom-regina.jpg",
                is_featured=True,
            )
        )
        await session.commit()

        provider = HubCatalogProvider(connection_id=556)
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
    restent exactement ceux du snapshot hub (ProductOverride n'expose aucune
    colonne prix/TVA -- ce test verrouille la propriete au niveau du provider)."""
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.models import ProductOverride
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=561,
            payload={},
            normalized=[
                NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5, tax_rate=0.055)
            ],
        )
        override = ProductOverride(
            connection_id=561,
            external_product_id="ext-1",
            description="Ma description maison",
            is_featured=True,
            display_order=1,
        )
        session.add(override)
        await session.commit()

        assert not hasattr(override, "price")
        assert not hasattr(override, "base_price")
        assert not hasattr(override, "tax_rate")

        provider = HubCatalogProvider(connection_id=561)
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


async def test_get_catalog_sorts_by_display_order_then_name():
    """Les produits avec un display_order explicite passent devant ; ceux sans
    override (ou sans display_order) sont ordonnes par nom, apres."""
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.models import ProductOverride
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=559,
            payload={},
            normalized=[
                NormalizedCatalogProduct(external_id="ext-a", name="Anchois", price=9.0),
                NormalizedCatalogProduct(external_id="ext-b", name="Bolognese", price=10.0),
                NormalizedCatalogProduct(external_id="ext-c", name="Calzone", price=12.0),
            ],
        )
        session.add(ProductOverride(connection_id=559, external_product_id="ext-c", display_order=1))
        session.add(ProductOverride(connection_id=559, external_product_id="ext-b", display_order=2))
        await session.commit()

        provider = HubCatalogProvider(connection_id=559)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 3
        assert [s.name for s in summaries] == ["Calzone", "Bolognese", "Anchois"]
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_paginates_in_memory():
    """La pagination porte sur le snapshot deja charge en memoire : total = taille
    totale du snapshot, la page ne contient que la tranche demandee."""
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=560,
            payload={},
            normalized=[
                NormalizedCatalogProduct(external_id=f"ext-{i}", name=f"Pizza {i}", price=10.0 + i)
                for i in range(5)
            ],
        )

        provider = HubCatalogProvider(connection_id=560)
        page1, total1 = await provider.get_catalog(session, PaginationParams(page=1, page_size=2))
        page2, total2 = await provider.get_catalog(session, PaginationParams(page=2, page_size=2))
        page3, total3 = await provider.get_catalog(session, PaginationParams(page=3, page_size=2))

        assert (total1, total2, total3) == (5, 5, 5)
        assert [s.name for s in page1] == ["Pizza 0", "Pizza 1"]
        assert [s.name for s in page2] == ["Pizza 2", "Pizza 3"]
        assert [s.name for s in page3] == ["Pizza 4"]
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_derives_stable_surrogate_ids():
    """L'id entier expose est derive de l'external_id (CRC32 31 bits) : stable
    d'un appel a l'autre, distinct entre produits."""
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=562,
            payload={},
            normalized=[
                NormalizedCatalogProduct(external_id="ext-1", name="Anchois", price=9.0),
                NormalizedCatalogProduct(external_id="ext-2", name="Bolognese", price=10.0),
            ],
        )

        provider = HubCatalogProvider(connection_id=562)
        first, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))
        second, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        ids = [s.id for s in first]
        assert ids == [s.id for s in second]
        assert len(set(ids)) == 2
        assert all(0 < product_id <= 0x7FFFFFFF for product_id in ids)
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
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_staleness_minutes", 60)
        snapshot = await snapshot_repository.upsert_snapshot(
            session,
            connection_id=557,
            payload={},
            normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5)],
        )
        snapshot.synced_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

        redis = AsyncMock()
        provider = HubCatalogProvider(connection_id=557)
        summaries, _ = await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        assert len(summaries) == 1  # still served despite being stale
        redis.enqueue_job.assert_awaited_once_with("sync_catalog_from_hub", connection_id=557)
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_still_served_when_stale_enqueue_fails(monkeypatch):
    """L'enqueue de resynchronisation est best-effort : une panne Redis ne doit
    jamais faire echouer la lecture du catalogue."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.core.config import settings as app_settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_staleness_minutes", 60)
        snapshot = await snapshot_repository.upsert_snapshot(
            session,
            connection_id=563,
            payload={},
            normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5)],
        )
        snapshot.synced_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

        redis = AsyncMock()
        redis.enqueue_job.side_effect = ConnectionError("redis down")
        provider = HubCatalogProvider(connection_id=563)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        assert total == 1
        assert summaries[0].name == "Regina"
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_stale_without_redis_still_serves_snapshot(monkeypatch):
    """Sans handle Redis (redis=None), un snapshot perime reste servi tel quel."""
    from datetime import datetime, timedelta, timezone

    from app.core.config import settings as app_settings
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        monkeypatch.setattr(app_settings, "pos_hub_snapshot_staleness_minutes", 60)
        snapshot = await snapshot_repository.upsert_snapshot(
            session,
            connection_id=564,
            payload={},
            normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5)],
        )
        snapshot.synced_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

        provider = HubCatalogProvider(connection_id=564)
        summaries, total = await provider.get_catalog(session, PaginationParams(page=1, page_size=10))

        assert total == 1
        assert summaries[0].name == "Regina"
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await session.close()
        await conn.rollback()
        await conn.close()
        await engine.dispose()


async def test_get_catalog_does_not_enqueue_resync_when_snapshot_is_fresh():
    from unittest.mock import AsyncMock

    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    try:
        engine, conn, session = await _tenant_session()
        await snapshot_repository.upsert_snapshot(
            session,
            connection_id=558,
            payload={},
            normalized=[NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5)],
        )

        redis = AsyncMock()
        provider = HubCatalogProvider(connection_id=558)
        await provider.get_catalog(session, PaginationParams(page=1, page_size=10), redis=redis)

        redis.enqueue_job.assert_not_awaited()
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
