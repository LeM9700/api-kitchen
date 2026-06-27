"""payments improvements for refunds, connect and expiration

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
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
        bind.execute(sa.text(f"ALTER TABLE {quoted}.payments ADD COLUMN IF NOT EXISTS provider_account_id VARCHAR(255)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.payments ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"))
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.refunds (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES {quoted}.orders(id),
                    payment_id INTEGER NOT NULL REFERENCES {quoted}.payments(id),
                    stripe_refund_id VARCHAR(128) NOT NULL UNIQUE,
                    amount INTEGER NOT NULL,
                    reason VARCHAR(256),
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_refunds_order_id_{slug} ON {quoted}.refunds (order_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_refunds_payment_id_{slug} ON {quoted}.refunds (payment_id)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.refunds ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(512)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.refunds ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER"))


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("refunds", "created_by_user_id", schema=schema)
        op.drop_column("refunds", "failure_reason", schema=schema)
        op.drop_column("payments", "expires_at", schema=schema)
        op.drop_column("payments", "provider_account_id", schema=schema)
