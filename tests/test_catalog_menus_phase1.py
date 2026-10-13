"""Phase 1 coverage for tenant-scoped catalog menu persistence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.modules.catalog.models import Menu, MenuGroup, MenuGroupItem


def test_menu_tables_are_part_of_tenant_metadata():
    assert Menu.__table__.name == "menus"
    assert MenuGroup.__table__.name == "menu_groups"
    assert MenuGroupItem.__table__.name == "menu_group_items"

    constraints = {
        constraint.name
        for table in (Menu.__table__, MenuGroup.__table__, MenuGroupItem.__table__)
        for constraint in table.constraints
        if constraint.name
    }
    assert "ck_menus_fixed_price_non_negative" in constraints
    assert "ck_menu_groups_min_choices_positive" in constraints
    assert "ck_menu_groups_choice_range" in constraints
    assert "uq_menu_group_items_group_product" in constraints


async def test_menu_hierarchy_can_reference_existing_product(db_session):
    product_id = (
        await db_session.execute(
            text(
                """INSERT INTO products (name, base_price, is_active)
                   VALUES ('Pizza test menu', 12.00, true) RETURNING id"""
            )
        )
    ).scalar_one()
    now = datetime.now(UTC)
    menu = Menu(
        name="Menu midi",
        description="Une formule complete",
        fixed_price=15.90,
        available_from=now,
        available_until=now + timedelta(days=30),
    )
    db_session.add(menu)
    await db_session.flush()

    group = MenuGroup(menu_id=menu.id, name="Pizza", min_choices=1, max_choices=1)
    db_session.add(group)
    await db_session.flush()

    db_session.add(MenuGroupItem(group_id=group.id, product_id=product_id))
    await db_session.flush()


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        ("INSERT INTO menus (name, fixed_price) VALUES ('Invalide', -0.01)", {}),
        (
            """INSERT INTO menus (name, fixed_price, available_from, available_until)
               VALUES ('Invalide', 10, :start, :end)""",
            {
                "start": datetime(2026, 10, 14, tzinfo=UTC),
                "end": datetime(2026, 10, 13, tzinfo=UTC),
            },
        ),
    ],
)
async def test_menu_database_constraints_reject_invalid_menu(db_session, statement, params):
    with pytest.raises(IntegrityError):
        await db_session.execute(text(statement), params)
        await db_session.flush()


async def test_menu_group_database_constraints_reject_invalid_range(db_session):
    menu_id = (
        await db_session.execute(
            text("INSERT INTO menus (name, fixed_price) VALUES ('Menu', 10) RETURNING id")
        )
    ).scalar_one()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                """INSERT INTO menu_groups (menu_id, name, min_choices, max_choices)
                   VALUES (:menu_id, 'Plat', 2, 1)"""
            ),
            {"menu_id": menu_id},
        )
        await db_session.flush()


async def test_menu_group_rejects_duplicate_product(db_session):
    product_id = (
        await db_session.execute(
            text("INSERT INTO products (name, base_price) VALUES ('Pizza', 12) RETURNING id")
        )
    ).scalar_one()
    menu_id = (
        await db_session.execute(
            text("INSERT INTO menus (name, fixed_price) VALUES ('Menu', 15) RETURNING id")
        )
    ).scalar_one()
    group_id = (
        await db_session.execute(
            text(
                """INSERT INTO menu_groups (menu_id, name, min_choices, max_choices)
                   VALUES (:menu_id, 'Plat', 1, 1) RETURNING id"""
            ),
            {"menu_id": menu_id},
        )
    ).scalar_one()
    await db_session.execute(
        text("INSERT INTO menu_group_items (group_id, product_id) VALUES (:group_id, :product_id)"),
        {"group_id": group_id, "product_id": product_id},
    )

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("INSERT INTO menu_group_items (group_id, product_id) VALUES (:group_id, :product_id)"),
            {"group_id": group_id, "product_id": product_id},
        )
        await db_session.flush()


async def test_menu_group_item_rejects_unknown_product(db_session):
    menu_id = (
        await db_session.execute(
            text("INSERT INTO menus (name, fixed_price) VALUES ('Menu', 15) RETURNING id")
        )
    ).scalar_one()
    group_id = (
        await db_session.execute(
            text(
                """INSERT INTO menu_groups (menu_id, name, min_choices, max_choices)
                   VALUES (:menu_id, 'Plat', 1, 1) RETURNING id"""
            ),
            {"menu_id": menu_id},
        )
    ).scalar_one()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("INSERT INTO menu_group_items (group_id, product_id) VALUES (:group_id, -1)"),
            {"group_id": group_id},
        )
        await db_session.flush()


def test_menu_models_expose_expected_foreign_key_delete_behaviour():
    group_fk = next(iter(inspect(MenuGroup).columns.menu_id.foreign_keys))
    item_group_fk = next(iter(inspect(MenuGroupItem).columns.group_id.foreign_keys))
    product_fk = next(iter(inspect(MenuGroupItem).columns.product_id.foreign_keys))

    assert group_fk.ondelete == "CASCADE"
    assert item_group_fk.ondelete == "CASCADE"
    assert product_fk.ondelete is None
