"""add processed_webhook_events table for Stripe webhook idempotency

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
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
                CREATE TABLE IF NOT EXISTS {quoted}.processed_webhook_events (
                    id SERIAL PRIMARY KEY,
                    stripe_event_id VARCHAR(255) NOT NULL UNIQUE,
                    event_type VARCHAR(128) NOT NULL,
                    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("processed_webhook_events", schema=schema)
