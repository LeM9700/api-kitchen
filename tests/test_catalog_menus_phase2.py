import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.auth.security import create_access_token
from app.core.http.errors import AppError
from app.main import app
from app.modules.admin.customers.models import AdminAuditLog
from app.modules.catalog import menu_service
from app.modules.catalog.menu_schemas import (
    MenuCreate,
    MenuGroupCreate,
    MenuGroupItemCreate,
    MenuGroupUpdate,
    MenuUpdate,
)
from app.modules.catalog.models import Menu, Product


ACTOR = {"id": 1, "email": "admin@test.com", "role": "admin"}
AUDIT_CONTEXT = {"actor": ACTOR, "ip_address": "127.0.0.1", "user_agent": "pytest"}


async def _product(db_session, name: str, *, active: bool = True, external_id: str | None = None) -> Product:
    product = Product(
        name=name,
        base_price=10,
        is_active=active,
        external_product_id=external_id,
    )
    db_session.add(product)
    await db_session.flush()
    return product


async def _draft_with_group(db_session, *, min_choices: int = 1):
    menu = await menu_service.create_menu(
        db_session,
        MenuCreate(name=f"Menu {uuid.uuid4().hex[:8]}", fixed_price=15.9),
        **AUDIT_CONTEXT,
    )
    menu = await menu_service.create_group(
        db_session,
        menu.id,
        MenuGroupCreate(name="Plat", min_choices=min_choices, max_choices=max(1, min_choices)),
        **AUDIT_CONTEXT,
    )
    return menu, menu.groups[0]


def test_menu_schemas_reject_invalid_values():
    with pytest.raises(ValidationError):
        MenuCreate(name=" ", fixed_price=10)
    with pytest.raises(ValidationError):
        MenuCreate(name="Menu", fixed_price=-1)
    with pytest.raises(ValidationError):
        MenuGroupCreate(name="Plat", min_choices=2, max_choices=1)


async def test_menu_must_be_created_as_draft(db_session):
    with pytest.raises(AppError) as exc_info:
        await menu_service.create_menu(
            db_session,
            MenuCreate(name="Menu actif trop tot", fixed_price=10, is_active=True),
            **AUDIT_CONTEXT,
        )
    assert exc_info.value.code == "MENU_NOT_PUBLISHABLE"


async def test_publishable_menu_accepts_pos_product_and_records_audit(db_session):
    product = await _product(db_session, "Pizza POS", external_id="pos-product-42")
    menu, group = await _draft_with_group(db_session)

    menu = await menu_service.add_group_item(
        db_session,
        menu.id,
        group.id,
        MenuGroupItemCreate(product_id=product.id),
        **AUDIT_CONTEXT,
    )
    assert menu.is_publishable is True
    assert menu.groups[0].items[0].product_external_id == "pos-product-42"

    menu = await menu_service.update_menu(
        db_session,
        menu.id,
        MenuUpdate(is_active=True, change_reason="Formule complete"),
        **AUDIT_CONTEXT,
    )
    assert menu.is_active is True

    actions = list(
        (
            await db_session.execute(
                select(AdminAuditLog.action)
                .where(AdminAuditLog.target_type == "menu", AdminAuditLog.target_id == str(menu.id))
                .order_by(AdminAuditLog.id)
            )
        ).scalars()
    )
    assert "menu_created" in actions
    assert "menu_composition_changed" in actions
    assert "menu_published" in actions


async def test_publication_rejects_group_without_enough_active_products(db_session):
    menu, _ = await _draft_with_group(db_session, min_choices=2)

    with pytest.raises(AppError) as exc_info:
        await menu_service.update_menu(
            db_session,
            menu.id,
            MenuUpdate(is_active=True),
            **AUDIT_CONTEXT,
        )
    assert exc_info.value.code == "MENU_NOT_PUBLISHABLE"
    assert "exige 2 choix" in exc_info.value.detail
    await db_session.rollback()


async def test_group_item_rejects_inactive_and_duplicate_products(db_session):
    inactive = await _product(db_session, "Produit inactif", active=False)
    active = await _product(db_session, "Produit actif")
    menu, group = await _draft_with_group(db_session)

    with pytest.raises(AppError) as inactive_error:
        await menu_service.add_group_item(
            db_session,
            menu.id,
            group.id,
            MenuGroupItemCreate(product_id=inactive.id),
            **AUDIT_CONTEXT,
        )
    assert inactive_error.value.code == "MENU_PRODUCT_INACTIVE"

    await menu_service.add_group_item(
        db_session,
        menu.id,
        group.id,
        MenuGroupItemCreate(product_id=active.id),
        **AUDIT_CONTEXT,
    )
    with pytest.raises(AppError) as duplicate_error:
        await menu_service.add_group_item(
            db_session,
            menu.id,
            group.id,
            MenuGroupItemCreate(product_id=active.id),
            **AUDIT_CONTEXT,
        )
    assert duplicate_error.value.code == "MENU_PRODUCT_ALREADY_IN_GROUP"


async def test_composition_change_auto_unpublishes_active_menu(db_session):
    product = await _product(db_session, "Pizza publication")
    menu, group = await _draft_with_group(db_session)
    menu = await menu_service.add_group_item(
        db_session,
        menu.id,
        group.id,
        MenuGroupItemCreate(product_id=product.id),
        **AUDIT_CONTEXT,
    )
    menu = await menu_service.update_menu(
        db_session,
        menu.id,
        MenuUpdate(is_active=True),
        **AUDIT_CONTEXT,
    )
    assert menu.is_active is True

    menu = await menu_service.update_group(
        db_session,
        menu.id,
        group.id,
        MenuGroupUpdate(display_order=2),
        **AUDIT_CONTEXT,
    )
    assert menu.is_active is False
    action = await db_session.scalar(
        select(AdminAuditLog.action)
        .where(
            AdminAuditLog.target_id == str(menu.id),
            AdminAuditLog.action == "menu_auto_unpublished",
        )
        .order_by(AdminAuditLog.id.desc())
    )
    assert action == "menu_auto_unpublished"


async def test_group_id_cannot_be_used_through_another_menu(db_session):
    first, first_group = await _draft_with_group(db_session)
    second, _ = await _draft_with_group(db_session)

    with pytest.raises(AppError) as exc_info:
        await menu_service.update_group(
            db_session,
            second.id,
            first_group.id,
            MenuGroupUpdate(name="Intrusion"),
            **AUDIT_CONTEXT,
        )
    assert exc_info.value.code == "MENU_GROUP_NOT_FOUND"
    assert first.id != second.id


async def test_price_change_has_dedicated_audit_event(db_session):
    menu = await menu_service.create_menu(
        db_session,
        MenuCreate(name="Menu prix", fixed_price=12),
        **AUDIT_CONTEXT,
    )
    await menu_service.update_menu(
        db_session,
        menu.id,
        MenuUpdate(fixed_price=14.5, change_reason="Hausse fournisseur"),
        **AUDIT_CONTEXT,
    )
    audit = await db_session.scalar(
        select(AdminAuditLog).where(
            AdminAuditLog.target_id == str(menu.id),
            AdminAuditLog.action == "menu_price_changed",
        )
    )
    assert audit is not None
    assert audit.metadata_json["old"] == 12.0
    assert audit.metadata_json["new"] == 14.5
    assert audit.metadata_json["reason"] == "Hausse fournisseur"


async def test_menu_admin_routes_require_authentication(client):
    response = await client.get("/api/v1/catalog/admin/menus")
    assert response.status_code == 401


async def test_staff_without_catalog_permission_is_denied(bootstrap_default_tenant):
    token = create_access_token(
        {
            "sub": str(bootstrap_default_tenant["staff_user_id"]),
            "email": "staff@test.com",
            "role": "staff",
            "tenant_id": bootstrap_default_tenant["tenant_id"],
            "tenant_slug": bootstrap_default_tenant["tenant_slug"],
            "permissions": [],
            "must_change_password": False,
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as staff_client:
        read_response = await staff_client.get("/api/v1/catalog/admin/menus")
        write_response = await staff_client.post(
            "/api/v1/catalog/admin/menus",
            json={"name": "Interdit", "fixed_price": 10},
        )
    assert read_response.status_code == 403
    assert write_response.status_code == 403


async def test_connected_tenant_can_create_local_menu(db_engine, authed_client, bootstrap_default_tenant):
    slug = bootstrap_default_tenant["tenant_slug"]
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE public.tenants SET integration_mode = 'connected' WHERE slug = :slug"),
            {"slug": slug},
        )
    try:
        response = await authed_client.post(
            "/api/v1/catalog/admin/menus",
            json={"name": f"Menu POS {uuid.uuid4().hex[:8]}", "fixed_price": 18.5},
        )
        assert response.status_code == 201, response.text
        assert response.json()["is_active"] is False
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE public.tenants SET integration_mode = 'standalone' WHERE slug = :slug"),
                {"slug": slug},
            )


async def test_delete_menu_removes_composition_and_keeps_audit(db_session):
    menu = await menu_service.create_menu(
        db_session,
        MenuCreate(name="Menu a supprimer", fixed_price=9),
        **AUDIT_CONTEXT,
    )
    await menu_service.delete_menu(db_session, menu.id, **AUDIT_CONTEXT)

    stored = await db_session.get(Menu, menu.id)
    assert stored is None
    audit = await db_session.scalar(
        select(AdminAuditLog.action).where(
            AdminAuditLog.target_id == str(menu.id),
            AdminAuditLog.action == "menu_deleted",
        )
    )
    assert audit == "menu_deleted"


async def test_unchanged_group_update_does_not_unpublish_menu(db_session):
    product = await _product(db_session, "Pizza stable")
    menu, group = await _draft_with_group(db_session)
    await menu_service.add_group_item(
        db_session,
        menu.id,
        group.id,
        MenuGroupItemCreate(product_id=product.id),
        **AUDIT_CONTEXT,
    )
    menu = await menu_service.update_menu(
        db_session,
        menu.id,
        MenuUpdate(is_active=True),
        **AUDIT_CONTEXT,
    )

    menu = await menu_service.update_group(
        db_session,
        menu.id,
        group.id,
        MenuGroupUpdate(name=group.name),
        **AUDIT_CONTEXT,
    )

    assert menu.is_active is True


async def test_admin_menu_api_complete_workflow(db_engine, authed_client, bootstrap_default_tenant):
    slug = bootstrap_default_tenant["tenant_slug"]
    product_name = f"Produit API {uuid.uuid4().hex[:8]}"
    async with db_engine.begin() as conn:
        await conn.execute(text(f'SET search_path TO "tenant_{slug}", public'))
        product_id = await conn.scalar(
            text(
                """INSERT INTO products (name, base_price, is_active)
                   VALUES (:name, 10, true)
                   RETURNING id"""
            ),
            {"name": product_name},
        )

    create_response = await authed_client.post(
        "/api/v1/catalog/admin/menus",
        json={"name": f"Menu API {uuid.uuid4().hex[:8]}", "fixed_price": 17.5},
    )
    assert create_response.status_code == 201, create_response.text
    menu_id = create_response.json()["id"]

    group_response = await authed_client.post(
        f"/api/v1/catalog/admin/menus/{menu_id}/groups",
        json={"name": "Plat", "min_choices": 1, "max_choices": 1},
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["groups"][0]["id"]

    item_response = await authed_client.post(
        f"/api/v1/catalog/admin/menus/{menu_id}/groups/{group_id}/items",
        json={"product_id": product_id},
    )
    assert item_response.status_code == 201, item_response.text
    assert item_response.json()["is_publishable"] is True

    publish_response = await authed_client.put(
        f"/api/v1/catalog/admin/menus/{menu_id}",
        json={"is_active": True, "change_reason": "Test API complet"},
    )
    assert publish_response.status_code == 200, publish_response.text
    assert publish_response.json()["is_active"] is True

    delete_response = await authed_client.delete(f"/api/v1/catalog/admin/menus/{menu_id}")
    assert delete_response.status_code == 204, delete_response.text
    get_response = await authed_client.get(f"/api/v1/catalog/admin/menus/{menu_id}")
    assert get_response.status_code == 404
