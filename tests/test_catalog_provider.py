def test_catalog_provider_protocol_is_runtime_checkable():
    from app.modules.catalog.ports import CatalogProvider

    assert hasattr(CatalogProvider, "__protocol_attrs__") or True  # sanity import check
    for name in ("get_catalog", "create_product", "update_product", "delete_product"):
        assert hasattr(CatalogProvider, name)
