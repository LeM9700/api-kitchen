import sqlalchemy as sa


async def test_get_override_returns_none_when_absent(db_session):
    from app.modules.catalog import override_repository

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await override_repository.get_override(db_session, product_id=999999)
    assert result is None


async def test_upsert_override_creates_then_updates(db_session):
    from app.modules.catalog import override_repository
    from app.modules.catalog.models import Product
    from app.modules.catalog.schemas import ProductOverrideCreate

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    product = Product(name="Regina", base_price=11.0, external_product_id="ext-ovr-1")
    db_session.add(product)
    await db_session.commit()

    created = await override_repository.upsert_override(
        db_session, product.id, ProductOverrideCreate(is_featured=True, display_order=1)
    )
    assert created.product_id == product.id
    assert created.is_featured is True

    updated = await override_repository.upsert_override(
        db_session, product.id, ProductOverrideCreate(is_featured=False, image_url="https://x/y.jpg")
    )
    assert updated.id == created.id  # same row, not a duplicate
    assert updated.is_featured is False
    assert updated.image_url == "https://x/y.jpg"


async def test_list_overrides_by_product_ids_keyed_correctly(db_session):
    from app.modules.catalog import override_repository
    from app.modules.catalog.models import Product
    from app.modules.catalog.schemas import ProductOverrideCreate

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    p1 = Product(name="Regina", base_price=11.0, external_product_id="ext-ovr-2")
    p2 = Product(name="Calzone", base_price=12.0, external_product_id="ext-ovr-3")
    db_session.add_all([p1, p2])
    await db_session.commit()
    await override_repository.upsert_override(db_session, p1.id, ProductOverrideCreate(is_featured=True))

    overrides = await override_repository.list_overrides_by_product_ids(db_session, [p1.id, p2.id])
    assert set(overrides.keys()) == {p1.id}
    assert overrides[p1.id].is_featured is True


async def test_list_overrides_by_product_ids_empty_list_returns_empty_dict(db_session):
    from app.modules.catalog import override_repository

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    assert await override_repository.list_overrides_by_product_ids(db_session, []) == {}


async def test_delete_override_returns_false_when_absent(db_session):
    from app.modules.catalog import override_repository

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    assert await override_repository.delete_override(db_session, product_id=999999) is False


async def test_delete_override_removes_row(db_session):
    from app.modules.catalog import override_repository
    from app.modules.catalog.models import Product
    from app.modules.catalog.schemas import ProductOverrideCreate

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    product = Product(name="Regina", base_price=11.0, external_product_id="ext-ovr-4")
    db_session.add(product)
    await db_session.commit()
    await override_repository.upsert_override(db_session, product.id, ProductOverrideCreate(is_featured=True))

    assert await override_repository.delete_override(db_session, product.id) is True
    assert await override_repository.get_override(db_session, product.id) is None
