"""Ajoute timezone configurable par tenant dans tenant_config.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
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
                "timezone",
                sa.String(64),
                nullable=False,
                server_default="Europe/Paris",
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "timezone", schema=schema)
