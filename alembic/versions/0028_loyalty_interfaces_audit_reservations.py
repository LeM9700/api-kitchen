"""loyalty interfaces audit reservations and stats support

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
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

        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_config ADD COLUMN IF NOT EXISTS points_to_euro_rate NUMERIC(10, 4) NOT NULL DEFAULT 0.0100"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_config ADD COLUMN IF NOT EXISTS max_cumulative_multiplier NUMERIC(6, 2) NOT NULL DEFAULT 20.00"))

        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS transaction_type VARCHAR(32)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS source VARCHAR(32)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS changed_by_user_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS order_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS reward_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS reservation_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ADD COLUMN IF NOT EXISTS metadata JSONB"))

        bind.execute(
            sa.text(
                f"""
                UPDATE {quoted}.loyalty_transactions
                SET
                    transaction_type = CASE
                        WHEN points_delta > 0 THEN 'earn'
                        WHEN reason = 'expired' THEN 'expire'
                        ELSE 'redeem'
                    END,
                    source = CASE
                        WHEN reason LIKE 'order_delivered_%' THEN 'order'
                        WHEN reason = 'expired' THEN 'system'
                        WHEN reason LIKE 'redeem_reward_%' THEN 'reward'
                        ELSE 'admin'
                    END
                WHERE transaction_type IS NULL OR source IS NULL
                """
            )
        )

        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ALTER COLUMN transaction_type SET DEFAULT 'adjustment'"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ALTER COLUMN source SET DEFAULT 'system'"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ALTER COLUMN transaction_type SET NOT NULL"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.loyalty_transactions ALTER COLUMN source SET NOT NULL"))

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.loyalty_point_reservations (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER NOT NULL,
                    points_reserved INTEGER NOT NULL,
                    discount_amount NUMERIC(10, 2) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'reserved',
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    confirmed_at TIMESTAMPTZ,
                    cancelled_at TIMESTAMPTZ
                )
                """
            )
        )
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_loyalty_reservations_user_status_{slug} ON {quoted}.loyalty_point_reservations (user_id, status)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_loyalty_reservations_order_{slug} ON {quoted}.loyalty_point_reservations (order_id)"))


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(f"ix_loyalty_reservations_order_{slug}", table_name="loyalty_point_reservations", schema=schema)
        op.drop_index(f"ix_loyalty_reservations_user_status_{slug}", table_name="loyalty_point_reservations", schema=schema)
        op.drop_table("loyalty_point_reservations", schema=schema)

        op.drop_column("loyalty_transactions", "metadata", schema=schema)
        op.drop_column("loyalty_transactions", "reservation_id", schema=schema)
        op.drop_column("loyalty_transactions", "reward_id", schema=schema)
        op.drop_column("loyalty_transactions", "order_id", schema=schema)
        op.drop_column("loyalty_transactions", "changed_by_user_id", schema=schema)
        op.drop_column("loyalty_transactions", "source", schema=schema)
        op.drop_column("loyalty_transactions", "transaction_type", schema=schema)

        op.drop_column("loyalty_config", "max_cumulative_multiplier", schema=schema)
        op.drop_column("loyalty_config", "points_to_euro_rate", schema=schema)
