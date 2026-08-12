import pytest
from pydantic import ValidationError


def test_normalized_catalog_product_requires_price():
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    with pytest.raises(ValidationError):
        NormalizedCatalogProduct(external_id="ext-1", name="Regina")


def test_normalized_catalog_product_accepts_tax_rate():
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    product = NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.5, tax_rate=0.1)
    assert product.price == 11.5
    assert product.tax_rate == 0.1


def test_product_override_create_rejects_price_field():
    from app.modules.catalog.schemas import ProductOverrideCreate

    with pytest.raises(ValidationError):
        ProductOverrideCreate(price=99.0)


def test_product_override_create_rejects_tax_rate_field():
    from app.modules.catalog.schemas import ProductOverrideCreate

    with pytest.raises(ValidationError):
        ProductOverrideCreate(tax_rate=0.2)


def test_product_override_create_accepts_presentation_fields_only():
    from app.modules.catalog.schemas import ProductOverrideCreate

    override = ProductOverrideCreate(
        image_url="https://example.com/pizza.jpg",
        description="Notre best-seller",
        is_featured=True,
        display_order=1,
    )
    assert override.is_featured is True


def test_product_out_tax_rate_and_is_featured_default_to_none_and_false():
    from app.modules.catalog.schemas import ProductOut

    product = ProductOut(id=1, name="Regina", base_price=11.0, is_active=True)
    assert product.tax_rate is None
    assert product.is_featured is False
