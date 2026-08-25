import pytest


def _hubrise_payload(**product_overrides) -> dict:
    product = {
        "id": "ext-1",
        "name": "Regina",
        "description": "Jambon, champignons",
        "category_id": "cat-pizzas",
        "tax_rate": {"delivery": 0.1, "collection": 0.055, "eat_in": 0.1},
        "skus": [{"id": "sku-1", "name": "Regina", "price": 11.5}],
    }
    product.update(product_overrides)
    return {
        "id": "catalog-1",
        "location_id": "loc-1",
        "data": {
            "categories": [{"id": "cat-pizzas", "name": "Pizzas"}],
            "products": [product],
        },
    }


def test_normalize_catalog_maps_hubrise_payload_to_pivot():
    from app.modules.catalog.normalize import normalize_catalog

    result = normalize_catalog(_hubrise_payload())

    assert len(result) == 1
    product = result[0]
    assert product.external_id == "ext-1"
    assert product.name == "Regina"
    assert product.category == "Pizzas"
    assert product.price == 11.5  # prix de la premiere SKU
    assert product.tax_rate == 0.1  # eat_in prioritaire


def test_normalize_catalog_tax_rate_falls_back_to_delivery_then_collection():
    from app.modules.catalog.normalize import normalize_catalog

    result = normalize_catalog(
        _hubrise_payload(tax_rate={"delivery": 0.2, "collection": 0.055})
    )
    assert result[0].tax_rate == 0.2

    result = normalize_catalog(_hubrise_payload(tax_rate={"collection": 0.055}))
    assert result[0].tax_rate == 0.055


def test_normalize_catalog_accepts_flat_tax_rate_for_forward_compat():
    """Si HubRise renvoie un jour un flottant simple plutot qu'un objet par
    canal, ne pas planter -- traiter comme le taux unique."""
    from app.modules.catalog.normalize import normalize_catalog

    result = normalize_catalog(_hubrise_payload(tax_rate=0.1))
    assert result[0].tax_rate == 0.1


def test_normalize_catalog_reads_is_active_from_sku_restrictions():
    """HubRise n'a pas de flag disabled/deleted au niveau produit --
    c'est skus[].restrictions.enabled (defaut true, omis si true)."""
    from app.modules.catalog.normalize import normalize_catalog

    result = normalize_catalog(
        _hubrise_payload(skus=[{"id": "sku-1", "price": 99.0, "restrictions": {"enabled": False}}])
    )
    assert result[0].is_active is False

    # Omis (comportement documente) -> actif par defaut.
    result = normalize_catalog(_hubrise_payload(skus=[{"id": "sku-1", "price": 11.5}]))
    assert result[0].is_active is True


def test_normalize_catalog_defaults_missing_optional_fields():
    from app.modules.catalog.normalize import normalize_catalog

    payload = {
        "data": {
            "categories": [],
            "products": [{"id": "ext-2", "name": "Margherita", "skus": [{"id": "s", "price": 9.0}]}],
        }
    }

    result = normalize_catalog(payload)

    assert result[0].description is None
    assert result[0].category is None
    assert result[0].tax_rate is None
    assert result[0].is_active is True


def test_normalize_catalog_skips_products_without_skus():
    """Un produit sans SKU n'a aucun prix determinable -- ignore plutot que
    de lui inventer un prix a 0."""
    from app.modules.catalog.normalize import normalize_catalog

    payload = _hubrise_payload()
    payload["data"]["products"][0]["skus"] = []

    result = normalize_catalog(payload)

    assert result == []


def test_normalize_catalog_raises_on_missing_data_key():
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError, normalize_catalog

    with pytest.raises(MalformedHubCatalogPayloadError):
        normalize_catalog({})


def test_normalize_catalog_raises_on_missing_products_key():
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError, normalize_catalog

    with pytest.raises(MalformedHubCatalogPayloadError):
        normalize_catalog({"data": {}})


def test_normalize_catalog_raises_on_missing_required_field():
    from app.modules.catalog.normalize import MalformedHubCatalogPayloadError, normalize_catalog

    payload = _hubrise_payload()
    del payload["data"]["products"][0]["name"]

    with pytest.raises(MalformedHubCatalogPayloadError):
        normalize_catalog(payload)
