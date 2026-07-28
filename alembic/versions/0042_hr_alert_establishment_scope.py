"""HR alerts: establishment scope for labor cost alerts

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
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
                f"ALTER TABLE IF EXISTS {quoted}.hr_alerts "
                "ALTER COLUMN employee_id DROP NOT NULL"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE IF EXISTS {quoted}.hr_alerts "
                f"ADD COLUMN IF NOT EXISTS establishment_id INTEGER REFERENCES {quoted}.establishments(id)"
            )
        )
        bind.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('{schema}.hr_alerts') IS NOT NULL THEN
                        CREATE INDEX IF NOT EXISTS ix_hr_alerts_establishment_type
                        ON {quoted}.hr_alerts (establishment_id, type);
                    END IF;
                END $$;
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'
        bind.execute(
            sa.text(f"DROP INDEX IF EXISTS {quoted}.ix_hr_alerts_establishment_type")
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE IF EXISTS {quoted}.hr_alerts "
                "DROP COLUMN IF EXISTS establishment_id"
            )
        )
        bind.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('{schema}.hr_alerts') IS NOT NULL THEN
                        DELETE FROM {quoted}.hr_alerts WHERE employee_id IS NULL;
                    END IF;
                END $$;
                """
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE IF EXISTS {quoted}.hr_alerts "
                "ALTER COLUMN employee_id SET NOT NULL"
            )
        )
