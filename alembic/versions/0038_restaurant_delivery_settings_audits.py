"""Add restaurant_delivery_settings_audits per tenant schema

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
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
            "restaurant_delivery_settings_audits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("changed_by_user_id", sa.Integer(), nullable=False),
            sa.Column("user_email", sa.String(255), nullable=True),
            sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("field_name", sa.String(255), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            schema=schema,
        )
        op.create_index(
            "ix_restaurant_delivery_settings_audits_changed_at",
            "restaurant_delivery_settings_audits",
            ["changed_at"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(
            "ix_restaurant_delivery_settings_audits_changed_at",
            table_name="restaurant_delivery_settings_audits",
            schema=schema,
        )
        op.drop_table("restaurant_delivery_settings_audits", schema=schema)
