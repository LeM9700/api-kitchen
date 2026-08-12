import pytest


def test_normalize_catalog_maps_hub_payload_to_pivot():
    from app.modules.catalog.normalize import normalize_catalog

    payload = {
        "products": [
            {
                "id": "ext-1",
                "name": "Regina",
                "description": "Jambon, champignons",
                "category": "Pizzas",
                "price": 11.5,
                "tax_rate": 0.1,
                "image_url": "https://example.com/regina.jpg",
                "is_active": True,
            }
        ]
    }

    result = normalize_catalog(payload)

    assert len(result) == 1
    assert result[0].external_id == "ext-1"
    assert result[0].name == "Regina"
    assert result[0].price == 11.5
    assert result[0].tax_rate == 0.1


def test_normalize_catalog_defaults_missing_optional_fields():
    from app.modules.catalog.normalize import normalize_catalog

    payload = {"products": [{"id": "ext-2", "name": "Margherita", "price": 9.0}]}

    result = normalize_catalog(payload)

    assert result[0].description is None
    assert result[0].tax_rate is None
    assert result[0].is_active is True


def test_normalize_catalog_raises_on_missing_products_key():
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError, normalize_catalog

    with pytest.raises(MalformedHubCatalogPayloadError):
        normalize_catalog({})


def test_normalize_catalog_raises_on_missing_required_field():
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError, normalize_catalog

    with pytest.raises(MalformedHubCatalogPayloadError):
        normalize_catalog({"products": [{"id": "ext-1", "name": "Regina"}]})  # missing price
