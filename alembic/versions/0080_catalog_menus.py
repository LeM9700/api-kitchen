"""Catalog menus, mandatory choice groups and allowed products.

Revision ID: 0080
Revises: haccp_merge_0079_p3_20261012
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op


revision = "0080"
down_revision = "haccp_merge_0079_p3_20261012"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    return [row[0] for row in bind.execute(sa.text("SELECT slug FROM public.tenants"))]


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


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "products"):
            continue

        if not _table_exists(bind, schema, "menus"):
            op.create_table(
                "menus",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("name", sa.String(255), nullable=False),
                sa.Column("description", sa.Text(), nullable=True),
                sa.Column("fixed_price", sa.Numeric(10, 2), nullable=False),
                sa.Column("image_url", sa.String(512), nullable=True),
                sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
                sa.Column("available_from", sa.DateTime(timezone=True), nullable=True),
                sa.Column("available_until", sa.DateTime(timezone=True), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.CheckConstraint("fixed_price >= 0", name="ck_menus_fixed_price_non_negative"),
                sa.CheckConstraint("char_length(btrim(name)) > 0", name="ck_menus_name_not_blank"),
                sa.CheckConstraint("display_order >= 0", name="ck_menus_display_order_non_negative"),
                sa.CheckConstraint(
                    "available_from IS NULL OR available_until IS NULL OR available_until > available_from",
                    name="ck_menus_availability_window",
                ),
                schema=schema,
            )
            op.create_index(
                "ix_menus_active_display_order",
                "menus",
                ["is_active", "display_order"],
                schema=schema,
            )

        if not _table_exists(bind, schema, "menu_groups"):
            op.create_table(
                "menu_groups",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("menu_id", sa.Integer(), nullable=False),
                sa.Column("name", sa.String(128), nullable=False),
                sa.Column("min_choices", sa.Integer(), nullable=False, server_default="1"),
                sa.Column("max_choices", sa.Integer(), nullable=False, server_default="1"),
                sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
                sa.ForeignKeyConstraint(
                    ["menu_id"], [f"{schema}.menus.id"], ondelete="CASCADE"
                ),
                sa.CheckConstraint("char_length(btrim(name)) > 0", name="ck_menu_groups_name_not_blank"),
                sa.CheckConstraint("min_choices >= 1", name="ck_menu_groups_min_choices_positive"),
                sa.CheckConstraint("max_choices >= min_choices", name="ck_menu_groups_choice_range"),
                sa.CheckConstraint("display_order >= 0", name="ck_menu_groups_display_order_non_negative"),
                schema=schema,
            )
            op.create_index(
                "ix_menu_groups_menu_display_order",
                "menu_groups",
                ["menu_id", "display_order"],
                schema=schema,
            )

        if not _table_exists(bind, schema, "menu_group_items"):
            op.create_table(
                "menu_group_items",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("group_id", sa.Integer(), nullable=False),
                sa.Column("product_id", sa.Integer(), nullable=False),
                sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
                sa.ForeignKeyConstraint(
                    ["group_id"], [f"{schema}.menu_groups.id"], ondelete="CASCADE"
                ),
                sa.ForeignKeyConstraint(["product_id"], [f"{schema}.products.id"]),
                sa.UniqueConstraint("group_id", "product_id", name="uq_menu_group_items_group_product"),
                sa.CheckConstraint(
                    "display_order >= 0", name="ck_menu_group_items_display_order_non_negative"
                ),
                schema=schema,
            )
            op.create_index(
                "ix_menu_group_items_group_display_order",
                "menu_group_items",
                ["group_id", "display_order"],
                schema=schema,
            )
            op.create_index(
                "ix_menu_group_items_product",
                "menu_group_items",
                ["product_id"],
                schema=schema,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for table_name in ("menu_group_items", "menu_groups", "menus"):
            if _table_exists(bind, schema, table_name):
                op.drop_table(table_name, schema=schema)
