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
