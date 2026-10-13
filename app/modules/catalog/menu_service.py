from decimal import Decimal
from math import ceil
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.admin.customers.service import record_admin_audit
from app.modules.catalog.menu_schemas import (
    MenuAdminListOut,
    MenuAdminOut,
    MenuGroupAdminOut,
    MenuGroupItemAdminOut,
)
from app.modules.catalog.models import Menu, MenuGroup, MenuGroupItem, Product


def _audit_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def _audit(
    session: AsyncSession,
    *,
    actor: dict,
    action: str,
    menu_id: int,
    metadata: dict[str, Any] | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    await record_admin_audit(
        session,
        actor=actor,
        action=action,
        target_type="menu",
        target_id=menu_id,
        metadata=metadata,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _publishability_errors(
    groups: list[MenuGroupAdminOut],
) -> list[str]:
    if not groups:
        return ["Ajoutez au moins un groupe de choix avant de publier le menu."]

    errors: list[str] = []
    for group in groups:
        active_items = [item for item in group.items if item.product_is_active]
        if len(active_items) < group.min_choices:
            errors.append(
                f"Le groupe '{group.name}' exige {group.min_choices} choix, "
                f"mais ne contient que {len(active_items)} produit(s) actif(s)."
            )
        inactive_names = [item.product_name for item in group.items if not item.product_is_active]
        if inactive_names:
            errors.append(
                f"Le groupe '{group.name}' contient des produits inactifs : " + ", ".join(inactive_names) + "."
            )
    return errors


async def _build_menu_outputs(session: AsyncSession, menus: list[Menu]) -> list[MenuAdminOut]:
    if not menus:
        return []

    menu_snapshots = [
        {
            "id": menu.id,
            "name": menu.name,
            "description": menu.description,
            "fixed_price": menu.fixed_price,
            "image_url": menu.image_url,
            "display_order": menu.display_order,
            "is_active": menu.is_active,
            "available_from": menu.available_from,
            "available_until": menu.available_until,
            "created_at": menu.created_at,
            "updated_at": menu.updated_at,
        }
        for menu in menus
    ]
    menu_ids = [menu["id"] for menu in menu_snapshots]
    group_rows = list(
        (
            await session.execute(
                select(MenuGroup)
                .where(MenuGroup.menu_id.in_(menu_ids))
                .order_by(MenuGroup.menu_id, MenuGroup.display_order, MenuGroup.id)
            )
        ).scalars()
    )
    group_ids = [group.id for group in group_rows]
    items_by_group: dict[int, list[MenuGroupItemAdminOut]] = {group_id: [] for group_id in group_ids}
    if group_ids:
        item_rows = (
            await session.execute(
                select(MenuGroupItem, Product)
                .join(Product, Product.id == MenuGroupItem.product_id)
                .where(MenuGroupItem.group_id.in_(group_ids))
                .order_by(MenuGroupItem.group_id, MenuGroupItem.display_order, MenuGroupItem.id)
            )
        ).all()
        for item, product in item_rows:
            items_by_group[item.group_id].append(
                MenuGroupItemAdminOut(
                    id=item.id,
                    group_id=item.group_id,
                    product_id=item.product_id,
                    display_order=item.display_order,
                    product_name=product.name,
                    product_is_active=product.is_active,
                    product_external_id=product.external_product_id,
                )
            )

    groups_by_menu: dict[int, list[MenuGroupAdminOut]] = {menu_id: [] for menu_id in menu_ids}
    for group in group_rows:
        groups_by_menu[group.menu_id].append(
            MenuGroupAdminOut(
                id=group.id,
                menu_id=group.menu_id,
                name=group.name,
                min_choices=group.min_choices,
                max_choices=group.max_choices,
                display_order=group.display_order,
                items=items_by_group[group.id],
            )
        )

    outputs: list[MenuAdminOut] = []
    for menu in menu_snapshots:
        groups = groups_by_menu[menu["id"]]
        errors = _publishability_errors(groups)
        outputs.append(
            MenuAdminOut(
                id=menu["id"],
                name=menu["name"],
                description=menu["description"],
                fixed_price=float(menu["fixed_price"]),
                image_url=menu["image_url"],
                display_order=menu["display_order"],
                is_active=menu["is_active"],
                available_from=menu["available_from"],
                available_until=menu["available_until"],
                created_at=menu["created_at"],
                updated_at=menu["updated_at"],
                groups=groups,
                is_publishable=not errors,
                validation_errors=errors,
            )
        )
    return outputs


async def _get_menu(session: AsyncSession, menu_id: int, *, for_update: bool = False) -> Menu:
    stmt = select(Menu).where(Menu.id == menu_id)
    if for_update:
        stmt = stmt.with_for_update()
    menu = await session.scalar(stmt)
    if menu is None:
        raise AppError("MENU_NOT_FOUND", f"Menu {menu_id} introuvable", 404, "menu_id")
    return menu


async def _get_group(session: AsyncSession, menu_id: int, group_id: int) -> MenuGroup:
    group = await session.scalar(
        select(MenuGroup).where(MenuGroup.id == group_id, MenuGroup.menu_id == menu_id).with_for_update()
    )
    if group is None:
        raise AppError("MENU_GROUP_NOT_FOUND", "Groupe de menu introuvable", 404, "group_id")
    return group


async def _get_item(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    item_id: int,
) -> tuple[MenuGroup, MenuGroupItem]:
    group = await _get_group(session, menu_id, group_id)
    item = await session.scalar(
        select(MenuGroupItem)
        .where(MenuGroupItem.id == item_id, MenuGroupItem.group_id == group.id)
        .with_for_update()
    )
    if item is None:
        raise AppError("MENU_GROUP_ITEM_NOT_FOUND", "Produit du groupe introuvable", 404, "item_id")
    return group, item


async def _menu_output(session: AsyncSession, menu: Menu) -> MenuAdminOut:
    return (await _build_menu_outputs(session, [menu]))[0]


async def _deactivate_for_composition_change(
    session: AsyncSession,
    menu: Menu,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> bool:
    if not menu.is_active:
        return False
    menu.is_active = False
    await _audit(
        session,
        actor=actor,
        action="menu_auto_unpublished",
        menu_id=menu.id,
        metadata={"reason": "composition_changed"},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return True


async def list_admin_menus(
    session: AsyncSession,
    pagination: PaginationParams,
) -> MenuAdminListOut:
    total = int(await session.scalar(select(func.count()).select_from(Menu)) or 0)
    menus = list(
        (
            await session.execute(
                select(Menu)
                .order_by(Menu.display_order, Menu.name, Menu.id)
                .offset((pagination.page - 1) * pagination.page_size)
                .limit(pagination.page_size)
            )
        ).scalars()
    )
    return MenuAdminListOut(
        items=await _build_menu_outputs(session, menus),
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        pages=max(1, ceil(total / pagination.page_size)),
    )


async def get_admin_menu(session: AsyncSession, menu_id: int) -> MenuAdminOut:
    return await _menu_output(session, await _get_menu(session, menu_id))


async def create_menu(
    session: AsyncSession,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    if body.is_active:
        raise AppError(
            "MENU_NOT_PUBLISHABLE",
            "Creez le menu en brouillon, puis ajoutez ses groupes et produits avant publication.",
            422,
            "is_active",
        )
    menu = Menu(**body.model_dump())
    session.add(menu)
    await session.flush()
    await _audit(
        session,
        actor=actor,
        action="menu_created",
        menu_id=menu.id,
        metadata={"fixed_price": float(menu.fixed_price)},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def update_menu(
    session: AsyncSession,
    menu_id: int,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    changes = body.model_dump(exclude_unset=True, exclude={"change_reason"})
    old_values = {key: _audit_value(getattr(menu, key)) for key in changes}

    effective_from = changes.get("available_from", menu.available_from)
    effective_until = changes.get("available_until", menu.available_until)
    if effective_from is not None and effective_until is not None and effective_until <= effective_from:
        raise AppError(
            "INVALID_MENU_AVAILABILITY",
            "La date de fin doit etre posterieure a la date de debut.",
            422,
            "available_until",
        )

    for key, value in changes.items():
        setattr(menu, key, value)

    if menu.is_active:
        output = await _menu_output(session, menu)
        if not output.is_publishable:
            raise AppError(
                "MENU_NOT_PUBLISHABLE",
                "Publication impossible : " + " ".join(output.validation_errors),
                422,
                "is_active",
            )

    new_values = {key: _audit_value(getattr(menu, key)) for key in changes}
    changed = {key: {"old": old_values[key], "new": new_values[key]} for key in changes if old_values[key] != new_values[key]}
    if changed:
        await _audit(
            session,
            actor=actor,
            action="menu_updated",
            menu_id=menu.id,
            metadata={"changes": changed, "reason": body.change_reason},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if "fixed_price" in changed:
            await _audit(
                session,
                actor=actor,
                action="menu_price_changed",
                menu_id=menu.id,
                metadata={**changed["fixed_price"], "reason": body.change_reason},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        if "is_active" in changed:
            await _audit(
                session,
                actor=actor,
                action="menu_published" if menu.is_active else "menu_unpublished",
                menu_id=menu.id,
                metadata={"reason": body.change_reason},
                ip_address=ip_address,
                user_agent=user_agent,
            )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def delete_menu(
    session: AsyncSession,
    menu_id: int,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    menu = await _get_menu(session, menu_id, for_update=True)
    await session.delete(menu)
    await _audit(
        session,
        actor=actor,
        action="menu_deleted",
        menu_id=menu.id,
        metadata={"groups_deleted": True},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()


async def create_group(
    session: AsyncSession,
    menu_id: int,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    group = MenuGroup(menu_id=menu.id, **body.model_dump())
    session.add(group)
    await session.flush()
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={"operation": "group_created", "group_id": group.id, "auto_unpublished": auto_unpublished},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def update_group(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    group = await _get_group(session, menu_id, group_id)
    requested_changes = body.model_dump(exclude_unset=True)
    changes = {
        key: value
        for key, value in requested_changes.items()
        if getattr(group, key) != value
    }
    effective_min = changes.get("min_choices", group.min_choices)
    effective_max = changes.get("max_choices", group.max_choices)
    if effective_max < effective_min:
        raise AppError(
            "INVALID_MENU_GROUP_RANGE",
            "max_choices doit etre superieur ou egal a min_choices.",
            422,
            "max_choices",
        )
    if not changes:
        return await _menu_output(session, menu)
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    for key, value in changes.items():
        setattr(group, key, value)
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={
            "operation": "group_updated",
            "group_id": group.id,
            "fields": sorted(changes),
            "auto_unpublished": auto_unpublished,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def delete_group(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    group = await _get_group(session, menu_id, group_id)
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    await session.delete(group)
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={"operation": "group_deleted", "group_id": group.id, "auto_unpublished": auto_unpublished},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def add_group_item(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    group = await _get_group(session, menu_id, group_id)
    product = await session.get(Product, body.product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Produit {body.product_id} introuvable", 404, "product_id")
    if not product.is_active:
        raise AppError(
            "MENU_PRODUCT_INACTIVE",
            f"Le produit '{product.name}' est inactif et ne peut pas etre ajoute au menu.",
            422,
            "product_id",
        )
    duplicate = await session.scalar(
        select(MenuGroupItem.id).where(
            MenuGroupItem.group_id == group.id,
            MenuGroupItem.product_id == product.id,
        )
    )
    if duplicate is not None:
        raise AppError(
            "MENU_PRODUCT_ALREADY_IN_GROUP",
            "Ce produit est deja present dans ce groupe.",
            409,
            "product_id",
        )
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    item = MenuGroupItem(group_id=group.id, **body.model_dump())
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise AppError(
            "MENU_PRODUCT_ALREADY_IN_GROUP",
            "Ce produit est deja present dans ce groupe.",
            409,
            "product_id",
        ) from exc
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={
            "operation": "product_added",
            "group_id": group.id,
            "item_id": item.id,
            "product_id": product.id,
            "product_external_id": product.external_product_id,
            "auto_unpublished": auto_unpublished,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def update_group_item(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    item_id: int,
    body,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    group, item = await _get_item(session, menu_id, group_id, item_id)
    if item.display_order == body.display_order:
        return await _menu_output(session, menu)
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    item.display_order = body.display_order
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={
            "operation": "product_reordered",
            "group_id": group.id,
            "item_id": item.id,
            "auto_unpublished": auto_unpublished,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)


async def delete_group_item(
    session: AsyncSession,
    menu_id: int,
    group_id: int,
    item_id: int,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> MenuAdminOut:
    menu = await _get_menu(session, menu_id, for_update=True)
    group, item = await _get_item(session, menu_id, group_id, item_id)
    product_id = item.product_id
    auto_unpublished = await _deactivate_for_composition_change(
        session, menu, actor=actor, ip_address=ip_address, user_agent=user_agent
    )
    await session.delete(item)
    await _audit(
        session,
        actor=actor,
        action="menu_composition_changed",
        menu_id=menu.id,
        metadata={
            "operation": "product_removed",
            "group_id": group.id,
            "item_id": item.id,
            "product_id": product_id,
            "auto_unpublished": auto_unpublished,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(menu)
    return await _menu_output(session, menu)
