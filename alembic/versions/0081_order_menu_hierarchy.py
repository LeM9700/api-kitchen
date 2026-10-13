"""Persist menu parent and component snapshots on order items.

Revision ID: 0081
Revises: 0080
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op


revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def _tenant_schemas(bind) -> list[str]:
    return [f"tenant_{row[0]}" for row in bind.execute(sa.text("SELECT slug FROM public.tenants"))]


def _table_exists(bind, schema: str, table_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM information_schema.tables
                   WHERE table_schema = :schema AND table_name = :table_name
                )"""
            ),
            {"schema": schema, "table_name": table_name},
        ).scalar()
    )


def _foreign_key_name(bind, schema: str, table_name: str, column_name: str) -> str | None:
    return bind.execute(
        sa.text(
            """SELECT tc.constraint_name
               FROM information_schema.table_constraints AS tc
               JOIN information_schema.key_column_usage AS kcu
                 ON tc.constraint_catalog = kcu.constraint_catalog
                AND tc.constraint_schema = kcu.constraint_schema
                AND tc.constraint_name = kcu.constraint_name
               WHERE tc.constraint_type = 'FOREIGN KEY'
                 AND tc.table_schema = :schema
                 AND tc.table_name = :table_name
                 AND kcu.column_name = :column_name
               LIMIT 1"""
        ),
        {
            "schema": schema,
            "table_name": table_name,
            "column_name": column_name,
        },
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    for schema in _tenant_schemas(bind):
        if not _table_exists(bind, schema, "order_items"):
            continue
        op.add_column(
            "order_items",
            sa.Column("item_type", sa.String(24), nullable=False, server_default="product"),
            schema=schema,
        )
        op.add_column("order_items", sa.Column("parent_item_id", sa.Integer()), schema=schema)
        op.add_column("order_items", sa.Column("menu_id", sa.Integer()), schema=schema)
        op.add_column("order_items", sa.Column("menu_name_snapshot", sa.String(255)), schema=schema)
        op.add_column("order_items", sa.Column("menu_group_id", sa.Integer()), schema=schema)
        op.add_column("order_items", sa.Column("menu_group_name_snapshot", sa.String(128)), schema=schema)
        op.add_column("order_items", sa.Column("menu_snapshot", sa.JSON()), schema=schema)
        op.add_column(
            "order_items",
            sa.Column("catalog_unit_price_snapshot", sa.Numeric(10, 2)),
            schema=schema,
        )
        op.alter_column("order_items", "product_id", nullable=True, schema=schema)
        op.create_foreign_key(
            "fk_order_items_parent_item_id",
            "order_items",
            "order_items",
            ["parent_item_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="CASCADE",
        )
        op.create_check_constraint(
            "ck_order_items_item_type",
            "order_items",
            "item_type IN ('product', 'menu', 'menu_component')",
            schema=schema,
        )
        op.create_check_constraint(
            "ck_order_items_type_shape",
            "order_items",
            "(item_type = 'product' AND product_id IS NOT NULL "
            "AND parent_item_id IS NULL AND menu_id IS NULL) OR "
            "(item_type = 'menu' AND product_id IS NULL "
            "AND parent_item_id IS NULL AND menu_id IS NOT NULL) OR "
            "(item_type = 'menu_component' AND product_id IS NOT NULL "
            "AND parent_item_id IS NOT NULL AND menu_id IS NOT NULL "
            "AND menu_group_id IS NOT NULL)",
            schema=schema,
        )
        op.create_index("ix_order_items_parent_item_id", "order_items", ["parent_item_id"], schema=schema)
        op.create_index("ix_order_items_menu_id", "order_items", ["menu_id"], schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for schema in _tenant_schemas(bind):
        if not _table_exists(bind, schema, "order_items"):
            continue
        menu_rows = bind.execute(
            sa.text(f'SELECT count(*) FROM "{schema}".order_items WHERE item_type != \'product\'')
        ).scalar()
        if menu_rows:
            raise RuntimeError(
                f"Cannot downgrade 0081: {schema}.order_items contains menu history"
            )
        op.drop_index("ix_order_items_menu_id", table_name="order_items", schema=schema)
        op.drop_index("ix_order_items_parent_item_id", table_name="order_items", schema=schema)
        op.drop_constraint("ck_order_items_type_shape", "order_items", type_="check", schema=schema)
        op.drop_constraint("ck_order_items_item_type", "order_items", type_="check", schema=schema)
        parent_fk_name = _foreign_key_name(
            bind,
            schema,
            "order_items",
            "parent_item_id",
        )
        if parent_fk_name is not None:
            op.drop_constraint(
                parent_fk_name,
                "order_items",
                type_="foreignkey",
                schema=schema,
            )
        op.alter_column("order_items", "product_id", nullable=False, schema=schema)
        for column in (
            "catalog_unit_price_snapshot",
            "menu_snapshot",
            "menu_group_name_snapshot",
            "menu_group_id",
            "menu_name_snapshot",
            "menu_id",
            "parent_item_id",
            "item_type",
        ):
            op.drop_column("order_items", column, schema=schema)
