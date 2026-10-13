import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.catalog import menu_service
from app.modules.catalog.allergen.allergen_models import (
    AllergenDefinition,
    DietaryTag,
    ProductAllergen,
    ProductDietaryTag,
)
from app.modules.catalog.image.image_model import MediaImage
from app.modules.catalog.models import (
    Extra,
    Menu,
    MenuGroup,
    MenuGroupItem,
    Product,
    ProductAvailabilityOverride,
    ProductExtra,
    ProductVariant,
)


async def _complete_menu(
    db_session,
    *,
    name: str,
    active: bool = True,
    product_active: bool = True,
    available_from: datetime | None = None,
    available_until: datetime | None = None,
):
    product = Product(
        name=f"Produit {uuid.uuid4().hex[:8]}",
        base_price=11,
        is_active=product_active,
    )
    menu = Menu(
        name=name,
        fixed_price=16.5,
        is_active=active,
        available_from=available_from,
        available_until=available_until,
    )
    db_session.add_all([product, menu])
    await db_session.flush()
    group = MenuGroup(menu_id=menu.id, name="Plat", min_choices=1, max_choices=1)
    db_session.add(group)
    await db_session.flush()
    item = MenuGroupItem(group_id=group.id, product_id=product.id)
    db_session.add(item)
    await db_session.flush()
    return menu, group, item, product


async def test_public_menu_payload_is_ready_for_client_ui(db_session):
    menu, _, item, product = await _complete_menu(db_session, name="Menu public enrichi")
    active_variant = ProductVariant(product_id=product.id, name="Grande", price_delta=2, is_active=True)
    inactive_variant = ProductVariant(product_id=product.id, name="Archivee", price_delta=1, is_active=False)
    active_extra = Extra(name=f"Extra {uuid.uuid4().hex[:8]}", price=1.5, is_active=True)
    inactive_extra = Extra(name=f"Extra inactif {uuid.uuid4().hex[:8]}", price=2, is_active=False)
    allergen = AllergenDefinition(
        name="Test allergene",
        slug=f"test-{uuid.uuid4().hex}",
        is_regulatory=False,
    )
    tag = DietaryTag(name="Test tag", slug=f"test-{uuid.uuid4().hex}")
    db_session.add_all([active_variant, inactive_variant, active_extra, inactive_extra, allergen, tag])
    await db_session.flush()
    db_session.add_all(
        [
            ProductExtra(product_id=product.id, extra_id=active_extra.id),
            ProductExtra(product_id=product.id, extra_id=inactive_extra.id),
            ProductAllergen(product_id=product.id, allergen_id=allergen.id, level="present", source="manual"),
            ProductDietaryTag(product_id=product.id, dietary_tag_id=tag.id),
            MediaImage(
                entity_type="product",
                entity_id=product.id,
                cloudinary_public_id=f"test/{uuid.uuid4().hex}",
                url="https://example.test/product.jpg",
                url_thumbnail="https://example.test/product-thumb.jpg",
                url_medium="https://example.test/product-medium.jpg",
                format="jpg",
                size_bytes=1000,
                width=1200,
                height=800,
                is_primary=True,
                display_order=0,
                alt_text="Produit du menu",
            ),
        ]
    )
    await db_session.flush()

    result = await menu_service.get_public_menu(db_session, menu.id)

    assert result.fixed_price == 16.5
    assert result.is_available is True
    assert len(result.groups) == 1
    public_product = result.groups[0].products[0]
    assert public_product.menu_group_item_id == item.id
    assert [variant.name for variant in public_product.variants] == ["Grande"]
    assert [extra.name for extra in public_product.extras] == [active_extra.name]
    assert public_product.gallery[0].url_medium.endswith("product-medium.jpg")
    assert public_product.allergens[0].slug == allergen.slug
    assert public_product.dietary_tags[0].slug == tag.slug
    assert public_product.availability.available is True


async def test_public_listing_hides_drafts_incomplete_and_out_of_window_menus(db_session):
    now = datetime.now(timezone.utc)
    visible, _, _, _ = await _complete_menu(db_session, name="Visible")
    await _complete_menu(db_session, name="Brouillon", active=False)
    await _complete_menu(db_session, name="Produit inactif", product_active=False)
    await _complete_menu(
        db_session,
        name="Pas encore disponible",
        available_from=now + timedelta(days=1),
    )
    incomplete = Menu(name="Sans groupe", fixed_price=10, is_active=True)
    db_session.add(incomplete)
    await db_session.flush()

    items, total = await menu_service.list_public_menus(
        db_session,
        PaginationParams(page=1, page_size=20),
        now=now,
    )

    assert total == 1
    assert [item.id for item in items] == [visible.id]


async def test_public_listing_paginates_after_completeness_filter(db_session):
    first, _, _, _ = await _complete_menu(db_session, name="Menu A")
    second, _, _, _ = await _complete_menu(db_session, name="Menu B")
    db_session.add(Menu(name="Menu incomplet entre les deux", fixed_price=10, is_active=True))
    await db_session.flush()

    items, total = await menu_service.list_public_menus(
        db_session,
        PaginationParams(page=2, page_size=1),
    )

    assert total == 2
    assert first.id != second.id
    assert [item.id for item in items] == [second.id]


async def test_operational_unavailability_is_exposed_without_leaking_admin_state(db_session):
    menu, _, _, product = await _complete_menu(db_session, name="Menu en rupture")
    db_session.add(
        ProductAvailabilityOverride(
            product_id=product.id,
            available=False,
            reason="Rupture temporaire",
            changed_by_user_id=1,
        )
    )
    await db_session.flush()

    result = await menu_service.get_public_menu(db_session, menu.id)

    assert result.is_available is False
    assert result.groups[0].is_available is False
    assert "assez de produits disponibles" in result.groups[0].unavailable_reason
    assert result.groups[0].products[0].availability.reason == "Rupture temporaire"
    assert not hasattr(result, "validation_errors")


async def test_public_detail_returns_not_found_for_incomplete_menu(db_session):
    menu = Menu(name="Menu incomplet", fixed_price=12, is_active=True)
    db_session.add(menu)
    await db_session.flush()

    with pytest.raises(AppError) as exc_info:
        await menu_service.get_public_menu(db_session, menu.id)

    assert exc_info.value.code == "MENU_NOT_FOUND"
    assert exc_info.value.status_code == 404


async def test_public_menu_routes_require_tenant_header(client):
    list_response = await client.get("/api/v1/catalog/menus")
    detail_response = await client.get("/api/v1/catalog/menus/1")

    assert list_response.status_code == 400
    assert list_response.json()["code"] == "MISSING_TENANT_SLUG"
    assert detail_response.status_code == 400


async def test_public_menu_listing_is_rate_limited(client):
    statuses = [
        (await client.get("/api/v1/catalog/menus")).status_code
        for _ in range(61)
    ]

    assert statuses[:60] == [400] * 60
    assert statuses[60] == 429


async def test_public_menu_routes_return_complete_payload_without_authentication(
    db_engine,
    client,
    bootstrap_default_tenant,
):
    slug = bootstrap_default_tenant["tenant_slug"]
    suffix = uuid.uuid4().hex[:8]
    async with db_engine.begin() as conn:
        await conn.execute(text(f'SET search_path TO "tenant_{slug}", public'))
        product_id = await conn.scalar(
            text(
                """INSERT INTO products (name, base_price, is_active)
                   VALUES (:name, 10, true)
                   RETURNING id"""
            ),
            {"name": f"Produit public {suffix}"},
        )
        menu_id = await conn.scalar(
            text(
                """INSERT INTO menus (name, fixed_price, is_active)
                   VALUES (:name, 18, true)
                   RETURNING id"""
            ),
            {"name": f"Menu public {suffix}"},
        )
        group_id = await conn.scalar(
            text(
                """INSERT INTO menu_groups (menu_id, name, min_choices, max_choices)
                   VALUES (:menu_id, 'Plat', 1, 1)
                   RETURNING id"""
            ),
            {"menu_id": menu_id},
        )
        await conn.execute(
            text(
                """INSERT INTO menu_group_items (group_id, product_id)
                   VALUES (:group_id, :product_id)"""
            ),
            {"group_id": group_id, "product_id": product_id},
        )

    headers = {"X-Tenant-Slug": slug}
    list_response = await client.get("/api/v1/catalog/menus", headers=headers)
    detail_response = await client.get(f"/api/v1/catalog/menus/{menu_id}", headers=headers)

    assert list_response.status_code == 200, list_response.text
    assert list_response.json()["total"] >= 1
    assert detail_response.status_code == 200, detail_response.text
    payload = detail_response.json()
    assert payload["id"] == menu_id
    assert payload["groups"][0]["products"][0]["id"] == product_id
    assert "validation_errors" not in payload
