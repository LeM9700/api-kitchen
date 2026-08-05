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
        quoted = f'"{schema}"'

        # CREATE TABLE IF NOT EXISTS : cf. 0035, un tenant peut deja avoir cette
        # table si son schema a ete provisionne apres coup avec un DDL plus recent
        # que la derniere fois que cette migration a tourne sur les tenants existants.
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.restaurant_delivery_settings (
                    id SERIAL PRIMARY KEY,
                    restaurant_lat DOUBLE PRECISION,
                    restaurant_lng DOUBLE PRECISION,
                    display_address TEXT,
                    independent_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    internal_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    pickup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    internal_delivery_fee NUMERIC(10, 2),
                    internal_delivery_minutes INTEGER,
                    internal_max_eta_minutes INTEGER,
                    restaurant_share_giveaway_points INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_restaurant_delivery_settings_share_giveaway_points
                        CHECK (restaurant_share_giveaway_points IN (0, 5, 10, 15))
                )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("restaurant_delivery_settings", schema=schema)
