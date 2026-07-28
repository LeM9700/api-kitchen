"""Add restaurant_delivery_settings per tenant schema

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.create_table(
            "restaurant_delivery_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("restaurant_lat", sa.Float(), nullable=True),
            sa.Column("restaurant_lng", sa.Float(), nullable=True),
            sa.Column("display_address", sa.Text(), nullable=True),
            sa.Column("independent_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("internal_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("pickup_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("internal_delivery_fee", sa.Numeric(10, 2), nullable=True),
            sa.Column("internal_delivery_minutes", sa.Integer(), nullable=True),
            sa.Column("internal_max_eta_minutes", sa.Integer(), nullable=True),
            sa.Column("restaurant_share_giveaway_points", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.CheckConstraint(
                "restaurant_share_giveaway_points IN (0, 5, 10, 15)",
                name="ck_restaurant_delivery_settings_share_giveaway_points",
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("restaurant_delivery_settings", schema=schema)
