"""Add HACCP DLC strict gate config

Revision ID: 0071
Revises: 0070
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa


revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column(
            "tenant_config",
            sa.Column(
                "haccp_dlc_gate_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "haccp_dlc_gate_enabled", schema=schema)
