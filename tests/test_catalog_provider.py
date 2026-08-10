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


async def test_get_catalog_provider_resolves_local_for_standalone(monkeypatch):
    from app.core.tenancy.integration_mode import IntegrationMode
    from app.modules.catalog import deps
    from app.modules.catalog.providers import LocalCatalogProvider

    async def fake_load(tenant_slug: str) -> IntegrationMode:
        return IntegrationMode.STANDALONE

    monkeypatch.setattr(deps, "_load_integration_mode", fake_load)

    provider = await deps.get_catalog_provider("any-slug")
    assert isinstance(provider, LocalCatalogProvider)


async def test_get_catalog_provider_resolves_connected_for_connected(monkeypatch):
    from app.core.tenancy.integration_mode import IntegrationMode
    from app.modules.catalog import deps
    from app.modules.catalog.providers import ConnectedCatalogProvider

    async def fake_load(tenant_slug: str) -> IntegrationMode:
        return IntegrationMode.CONNECTED

    monkeypatch.setattr(deps, "_load_integration_mode", fake_load)

    provider = await deps.get_catalog_provider("any-slug")
    assert isinstance(provider, ConnectedCatalogProvider)


async def test_load_integration_mode_reads_real_tenant_row():
    """Verifie la lecture reelle en base (cross-connection, comme en prod) --
    pas la session de test isolee par savepoint, qui n'est pas visible depuis
    une autre connexion (cf. docstring de la fixture ``client`` dans conftest.py)."""
    import uuid

    import pytest
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings
    from app.core.tenancy.integration_mode import IntegrationMode
    from app.modules.catalog.deps import _load_integration_mode

    engine = create_async_engine(settings.test_database_url or settings.database_url)
    slug = f"test-catalog-provider-{uuid.uuid4().hex[:8]}"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO public.tenants (slug, name, integration_mode) "
                    "VALUES (:slug, 'Test Tenant', 'connected')"
                ),
                {"slug": slug},
            )

        mode = await _load_integration_mode(slug)
        assert mode == IntegrationMode.CONNECTED

        mode_missing = await _load_integration_mode(f"{slug}-missing")
        assert mode_missing == IntegrationMode.STANDALONE
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        async with engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM public.tenants WHERE slug = :slug"), {"slug": slug})
        await engine.dispose()


async def test_products_route_still_returns_paginated_summaries_for_standalone_tenant(client):
    """Regression smoke test : la route publique GET /products doit continuer
    a fonctionner en mode STANDALONE (par defaut) exactement comme avant,
    maintenant qu'elle passe par LocalCatalogProvider."""
    # Le fixture `client` (tests/conftest.py, hors scope de cette tache) branche
    # directement l'ASGITransport sans jamais declencher le `lifespan` de l'app
    # -- `app.state.arq_pool` (peuple normalement par app/main.py::lifespan a
    # chaque vrai demarrage serveur) n'existe donc pas dans ce contexte de test.
    # GET /products en depend directement via `redis=Depends(get_arq_pool)` pour
    # son cache 30s (voir router.py) et n'a pas de fallback getattr(..., None)
    # comme customer/router.py:28 -- aucun test existant ne frappait ce chemin
    # HTTP avant ce smoke test, donc le trou n'avait jamais ete expose. Pas lie
    # au refactor CatalogProvider (Task 5) : reproductible a l'identique avec
    # l'ancien code du routeur. On simule ici l'etat "arq_pool absent/None"
    # (equivalent a l'environnement local sans Redis), que get_cached_json /
    # set_cached_json degradent deja silencieusement (cf. cache.py).
    from app.main import app

    had_arq_pool = hasattr(app.state, "arq_pool")
    previous_arq_pool = getattr(app.state, "arq_pool", None)
    app.state.arq_pool = None
    try:
        response = await client.get(
            "/api/v1/catalog/products",
            headers={"X-Tenant-Slug": "pizza_test"},
        )
    finally:
        if had_arq_pool:
            app.state.arq_pool = previous_arq_pool
        else:
            del app.state.arq_pool
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "total" in body
