"""Allow one ingredient lot to be tracked independently per establishment.

Revision ID: haccp_p3_20261004
Revises: haccp_p1_20261003
"""

from alembic import op
import sqlalchemy as sa


revision = "haccp_p3_20261004"
down_revision = "haccp_p1_20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $migration$
            DECLARE schema_name text;
            BEGIN
              FOR schema_name IN
                SELECT nspname FROM pg_namespace
                WHERE nspname LIKE 'tenant\\_%'
              LOOP
                IF to_regclass(format('%I.stock_batch_positions', schema_name)) IS NOT NULL THEN
                  EXECUTE format('ALTER TABLE %I.stock_batch_positions DROP CONSTRAINT stock_batch_positions_pkey', schema_name);
                  EXECUTE format('ALTER TABLE %I.stock_batch_positions ADD CONSTRAINT stock_batch_positions_pkey PRIMARY KEY (batch_id, establishment_id, ingredient_id)', schema_name);
                END IF;
              END LOOP;
            END $migration$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $migration$
            DECLARE schema_name text;
            BEGIN
              FOR schema_name IN
                SELECT nspname FROM pg_namespace
                WHERE nspname LIKE 'tenant\\_%'
              LOOP
                IF to_regclass(format('%I.stock_batch_positions', schema_name)) IS NOT NULL THEN
                  EXECUTE format('ALTER TABLE %I.stock_batch_positions DROP CONSTRAINT stock_batch_positions_pkey', schema_name);
                  EXECUTE format('ALTER TABLE %I.stock_batch_positions ADD CONSTRAINT stock_batch_positions_pkey PRIMARY KEY (batch_id)', schema_name);
                END IF;
              END LOOP;
            END $migration$;
            """
        )
    )
