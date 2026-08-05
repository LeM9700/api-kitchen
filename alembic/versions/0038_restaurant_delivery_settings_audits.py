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
        quoted = f'"{schema}"'

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.restaurant_delivery_settings_audits (
                    id SERIAL PRIMARY KEY,
                    changed_by_user_id INTEGER NOT NULL,
                    user_email VARCHAR(255),
                    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    field_name VARCHAR(255) NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    ip_address VARCHAR(45),
                    user_agent TEXT
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_restaurant_delivery_settings_audits_changed_at "
                f"ON {quoted}.restaurant_delivery_settings_audits (changed_at)"
            )
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
