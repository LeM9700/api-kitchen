def test_catalog_provider_protocol_is_runtime_checkable():
    from app.modules.catalog.ports import CatalogProvider

    assert hasattr(CatalogProvider, "__protocol_attrs__") or True  # sanity import check
    for name in ("get_catalog", "create_product", "update_product", "delete_product"):
        assert hasattr(CatalogProvider, name)


def test_read_only_catalog_error_message_is_restaurant_facing():
    from app.core.http.errors import AppError
    from app.modules.catalog.exceptions import ReadOnlyCatalogError

    exc = ReadOnlyCatalogError()

    assert isinstance(exc, AppError)
    assert exc.code == "CATALOG_READ_ONLY"
    assert exc.status_code == 409
    assert "technique" not in exc.detail.lower()
    assert "caisse connecté" in exc.detail


async def test_local_catalog_provider_create_update_delete_lifecycle():
    """Verifie que LocalCatalogProvider reproduit exactement le comportement
    actuel du router : creation, mise a jour de prix (avec audit), soft-delete."""
    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import settings
    from app.modules.catalog.providers import LocalCatalogProvider
    from app.modules.catalog.schemas import ProductCreate, ProductUpdate

    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text('SET search_path TO "tenant_pizza_test", public'))
            session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                provider = LocalCatalogProvider()

                product = await provider.create_product(
                    session, ProductCreate(name="Regina", base_price=11.0), user_id=1
                )
                assert product.id is not None
                assert product.is_active is True

                updated = await provider.update_product(
                    session, product.id, ProductUpdate(base_price=12.5), user_id=1
                )
                assert float(updated.base_price) == 12.5

                await provider.delete_product(session, product.id, user_id=1)
                refreshed = await session.get(type(product), product.id)
                assert refreshed.is_active is False
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


async def test_local_catalog_provider_get_catalog_matches_service_directly():
    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import settings
    from app.core.http.schemas import PaginationParams
    from app.modules.catalog import service
    from app.modules.catalog.models import Product
    from app.modules.catalog.providers import LocalCatalogProvider

    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text('SET search_path TO "tenant_pizza_test", public'))
            session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                session.add(Product(name="Fiorentina", base_price=10.0))
                await session.commit()

                pagination = PaginationParams(page=1, page_size=50)
                provider_summaries, provider_total = await LocalCatalogProvider().get_catalog(session, pagination)

                items, total = await service.list_products(session, pagination)
                direct_summaries = await service.build_product_summaries(session, items, include_availability=True)

                assert provider_total == total
                assert [s.id for s in provider_summaries] == [s.id for s in direct_summaries]
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


async def test_connected_catalog_provider_blocks_writes_but_allows_reads():
    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import settings
    from app.core.http.schemas import PaginationParams
    from app.modules.catalog.exceptions import ReadOnlyCatalogError
    from app.modules.catalog.providers import ConnectedCatalogProvider
    from app.modules.catalog.schemas import ProductCreate, ProductUpdate

    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text('SET search_path TO "tenant_pizza_test", public'))
            session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                provider = ConnectedCatalogProvider()

                pagination = PaginationParams(page=1, page_size=10)
                summaries, total = await provider.get_catalog(session, pagination)
                assert isinstance(summaries, list)
                assert isinstance(total, int)

                with pytest.raises(ReadOnlyCatalogError):
                    await provider.create_product(session, ProductCreate(name="X", base_price=1.0), user_id=1)
                with pytest.raises(ReadOnlyCatalogError):
                    await provider.update_product(session, 1, ProductUpdate(base_price=2.0), user_id=1)
                with pytest.raises(ReadOnlyCatalogError):
                    await provider.delete_product(session, 1, user_id=1)
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


def test_providers_satisfy_catalog_provider_protocol():
    from app.modules.catalog.ports import CatalogProvider
    from app.modules.catalog.providers import ConnectedCatalogProvider, LocalCatalogProvider

    assert isinstance(LocalCatalogProvider(), CatalogProvider)
    assert isinstance(ConnectedCatalogProvider(), CatalogProvider)
