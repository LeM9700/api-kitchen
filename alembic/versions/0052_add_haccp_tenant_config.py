"""Add HACCP feature flags to tenant_config

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-21

Ajouts dans chaque schema tenant (table tenant_config) :
- haccp_frying_oil_enabled : active le module suivi huile friteuse (optionnel)
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        bind.execute(
            sa.text(
                f'ALTER TABLE "{schema}".tenant_config '
                "ADD COLUMN IF NOT EXISTS haccp_frying_oil_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        bind.execute(
            sa.text(
                f'ALTER TABLE "{schema}".tenant_config '
                "DROP COLUMN IF EXISTS haccp_frying_oil_enabled"
            )
        )
