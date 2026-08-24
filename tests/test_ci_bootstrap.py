import sqlalchemy as sa


def test_catalog_package_reexports_image_service():
    from app.modules.catalog import image_service as package_image_service
    from app.modules.catalog.image import image_service as nested_image_service

    assert package_image_service is nested_image_service


async def test_db_session_defaults_to_seeded_tenant(db_session):
    schema = await db_session.scalar(sa.text("SELECT current_schema()"))
    establishment_count = await db_session.scalar(sa.text("SELECT COUNT(*) FROM establishments"))

    assert schema == "tenant_pizza_test"
    assert establishment_count >= 1
