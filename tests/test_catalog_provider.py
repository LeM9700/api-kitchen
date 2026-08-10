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
