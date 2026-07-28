async def test_delete_extra_blocked_while_linked_to_product():
    """P2-11/16 : DELETE /catalog/extras/{id} doit refuser (409) tant que l'extra
    est encore lie a un produit, puis reussir une fois delie."""
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import settings
    from app.core.http.errors import AppError
    from app.modules.catalog import service
    from app.modules.catalog.models import Extra, Product, ProductExtra

    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            from sqlalchemy import text

            await conn.execute(text('SET search_path TO "tenant_default", public'))
            session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                product = Product(name="Margherita", base_price=9.5)
                extra = Extra(name="Olives", price=1.5)
                session.add_all([product, extra])
                await session.flush()
                session.add(ProductExtra(product_id=product.id, extra_id=extra.id))
                await session.commit()

                with pytest.raises(AppError) as exc_info:
                    await service.delete_extra(session, extra.id)
                assert exc_info.value.code == "EXTRA_STILL_LINKED"

                await service.unlink_extra_from_product(session, product.id, extra.id)
                await service.delete_extra(session, extra.id)

                assert await session.get(Extra, extra.id) is None
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


async def test_availability_map_batches_single_query(monkeypatch):
    """Regression guard PB-01/P1-06 : le listing catalogue ne doit emettre qu'un
    seul appel de disponibilite stock, quel que soit le nombre de produits sur
    la page (au lieu d'un appel par produit)."""
    from app.modules.catalog import service
    from app.modules.stock import service as stock_service

    calls: list[list[int]] = []

    async def fake_get_products_availability(session, product_ids):
        calls.append(list(product_ids))
        return {
            pid: {"product_id": pid, "available": True, "limiting_ingredient": None}
            for pid in product_ids
        }

    monkeypatch.setattr(stock_service, "get_products_availability", fake_get_products_availability)

    product_ids = list(range(1, 101))
    availability = await service._availability_map(None, product_ids)

    assert len(calls) == 1
    assert calls[0] == product_ids
    assert len(availability) == 100
    assert all(out.available for out in availability.values())


async def test_create_product_requires_admin(client):
    response = await client.post(
        "/api/v1/catalog/products",
        json={"name": "Margherita", "base_price": 9.5},
    )
    assert response.status_code == 401


def test_catalog_paginated_response_exposes_total_count():
    from app.core.http.schemas import PaginationParams
    from app.modules.catalog.schemas import CatalogPaginatedResponse

    response = CatalogPaginatedResponse.build(
        items=[{"id": 1}],
        total=42,
        pagination=PaginationParams(page=2, page_size=10),
    )

    assert response.total == 42
    assert response.total_count == 42
    assert response.pages == 5


def test_catalog_csv_validation_accepts_core_rows():
    from app.modules.catalog import service

    csv_text = (
        "type,name,base_price,product_name,extra_name,price\n"
        "category,Pizzas,,,,\n"
        "product,Margherita,9.50,,,\n"
        "extra,,,,Olives,1.50\n"
        "product_extra,,9.50,Margherita,Olives,\n"
    )

    rows = service._read_csv_rows(csv_text)
    previews, errors = service._validate_csv_rows(rows)

    assert errors == []
    assert [preview.entity_type for preview in previews] == [
        "category",
        "product",
        "extra",
        "product_extra",
    ]


def test_product_inherits_preparation_station_from_category():
    from app.modules.catalog import service
    from app.modules.catalog.models import Category, Product

    category = Category(id=1, name="Boissons", display_order=0, preparation_station="counter")
    product = Product(id=2, category_id=1, name="Cola", base_price=2.5, preparation_station=None)

    assert service._effective_preparation_station(product, category) == "counter"


def test_product_preparation_station_overrides_category():
    from app.modules.catalog import service
    from app.modules.catalog.models import Category, Product

    category = Category(id=1, name="Boissons", display_order=0, preparation_station="counter")
    product = Product(id=2, category_id=1, name="Pain", base_price=2.5, preparation_station="kitchen")

    assert service._effective_preparation_station(product, category) == "kitchen"


def test_availability_override_requires_reason_when_unavailable():
    import pytest
    from pydantic import ValidationError

    from app.modules.catalog.schemas import ProductAvailabilityOverrideCreate

    with pytest.raises(ValidationError) as exc_info:
        ProductAvailabilityOverrideCreate(available=False)

    assert "reason" in str(exc_info.value)


async def test_availability_map_applies_latest_unavailable_override(monkeypatch):
    from app.modules.catalog import service
    from app.modules.catalog.models import ProductAvailabilityOverride
    from app.modules.stock import service as stock_service

    async def fake_get_products_availability(session, product_ids):
        return {1: {"product_id": 1, "available": True, "limiting_ingredient": None}}

    async def fake_latest_overrides(session, product_ids):
        return {
            1: ProductAvailabilityOverride(
                id=10,
                product_id=1,
                available=False,
                reason="Rupture pate",
                changed_by_user_id=7,
            )
        }

    monkeypatch.setattr(stock_service, "get_products_availability", fake_get_products_availability)
    monkeypatch.setattr(service, "_latest_availability_overrides", fake_latest_overrides)

    availability = await service._availability_map(object(), [1])

    assert availability[1].available is False
    assert availability[1].reason == "Rupture pate"
    assert availability[1].is_overridden is True
