"""P1/P2 admin-staff API contracts

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
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

        bind.execute(sa.text(f"ALTER TABLE {quoted}.users ADD COLUMN IF NOT EXISTS permissions JSON"))

        bind.execute(
            sa.text(
                f"""
                ALTER TABLE {quoted}.tenant_config
                ADD COLUMN IF NOT EXISTS large_stock_adjustment_threshold NUMERIC(12,3) NOT NULL DEFAULT 10,
                ADD COLUMN IF NOT EXISTS print_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS print_config JSON
                """
            )
        )

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.ingredient_batches (
                    id SERIAL PRIMARY KEY,
                    ingredient_id INTEGER NOT NULL REFERENCES {quoted}.ingredients(id),
                    quantity NUMERIC(12,3) NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ,
                    opened_at TIMESTAMPTZ,
                    use_within_hours_after_opening INTEGER,
                    status VARCHAR(16) NOT NULL DEFAULT 'sealed',
                    created_by_user_id INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_ingredient_batches_status
                        CHECK (status IN ('sealed', 'opened', 'expired', 'consumed', 'discarded'))
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_ingredient_batches_ingredient "
                f"ON {quoted}.ingredient_batches (ingredient_id)"
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_ingredient_batches_expires_at "
                f"ON {quoted}.ingredient_batches (expires_at)"
            )
        )

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.stock_adjustment_requests (
                    id SERIAL PRIMARY KEY,
                    ingredient_id INTEGER NOT NULL REFERENCES {quoted}.ingredients(id),
                    quantity_delta NUMERIC(12,3) NOT NULL,
                    reason VARCHAR(64) NOT NULL,
                    note TEXT,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    requested_by_user_id INTEGER NOT NULL,
                    reviewed_by_user_id INTEGER,
                    reviewed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_stock_adjustment_requests_status
                        CHECK (status IN ('pending', 'approved', 'rejected'))
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_status "
                f"ON {quoted}.stock_adjustment_requests (status)"
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_ingredient "
                f"ON {quoted}.stock_adjustment_requests (ingredient_id)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'

        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.stock_adjustment_requests"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.ingredient_batches"))

        bind.execute(
            sa.text(
                f"""
                ALTER TABLE {quoted}.tenant_config
                DROP COLUMN IF EXISTS print_config,
                DROP COLUMN IF EXISTS print_enabled,
                DROP COLUMN IF EXISTS large_stock_adjustment_threshold
                """
            )
        )
        bind.execute(sa.text(f"ALTER TABLE {quoted}.users DROP COLUMN IF EXISTS permissions"))
