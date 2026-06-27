"""add last_alert_sent_at to ingredients

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    # --- tenant_{slug}.ingredients : last_alert_sent_at ---
    # [⚠️ PROD] Même pattern multi-tenant que 0004 : itération sur les tenants existants.
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.add_column(
            "ingredients",
            sa.Column("last_alert_sent_at", sa.DateTime(timezone=True), nullable=True),
            schema=f"tenant_{slug}",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.drop_column("ingredients", "last_alert_sent_at", schema=f"tenant_{slug}")
