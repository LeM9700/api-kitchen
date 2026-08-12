async def test_catalog_snapshot_and_product_override_tables_exist(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    await db_session.execute(sa.text("SELECT id, connection_id, payload, normalized, synced_at FROM catalog_snapshots LIMIT 0"))
    await db_session.execute(
        sa.text(
            "SELECT id, connection_id, external_product_id, image_url, description, "
            "is_featured, display_order FROM product_overrides LIMIT 0"
        )
    )


async def test_get_snapshot_returns_none_when_absent(db_session):
    import sqlalchemy as sa

    from app.modules.catalog import snapshot_repository

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await snapshot_repository.get_snapshot(db_session, connection_id=999999)
    assert result is None


async def test_upsert_snapshot_creates_then_updates(db_session):
    import sqlalchemy as sa

    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.schemas import NormalizedCatalogProduct

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))

    first = [NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=11.0)]
    created = await snapshot_repository.upsert_snapshot(db_session, connection_id=42, payload={"v": 1}, normalized=first)
    assert created.connection_id == 42
    assert created.normalized[0]["external_id"] == "ext-1"

    second = [NormalizedCatalogProduct(external_id="ext-1", name="Regina", price=12.0)]
    updated = await snapshot_repository.upsert_snapshot(db_session, connection_id=42, payload={"v": 2}, normalized=second)
    assert updated.id == created.id
    assert updated.normalized[0]["price"] == 12.0

    fetched = await snapshot_repository.get_snapshot(db_session, connection_id=42)
    assert fetched.payload == {"v": 2}


async def test_list_overrides_keyed_by_external_product_id(db_session):
    import sqlalchemy as sa

    from app.modules.catalog.models import ProductOverride
    from app.modules.catalog import snapshot_repository

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    db_session.add(ProductOverride(connection_id=42, external_product_id="ext-1", is_featured=True))
    await db_session.commit()

    overrides = await snapshot_repository.list_overrides(db_session, connection_id=42)
    assert overrides["ext-1"].is_featured is True


async def test_product_and_product_override_have_materialization_columns(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    await db_session.execute(
        sa.text("SELECT id, external_product_id, tax_rate FROM products LIMIT 0")
    )
    await db_session.execute(
        sa.text(
            "SELECT id, product_id, image_url, description, is_featured, display_order "
            "FROM product_overrides LIMIT 0"
        )
    )
