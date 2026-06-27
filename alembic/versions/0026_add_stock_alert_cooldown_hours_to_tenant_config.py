"""add stock_alert_cooldown_hours to tenant_config

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.add_column(
            "tenant_config",
            sa.Column(
                "stock_alert_cooldown_hours",
                sa.Integer(),
                nullable=False,
                server_default="4",
            ),
            schema=f"tenant_{slug}",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.drop_column("tenant_config", "stock_alert_cooldown_hours", schema=f"tenant_{slug}")
