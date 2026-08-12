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
