"""add user_id audit column to stock_movements

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.add_column(
            "stock_movements",
            sa.Column("user_id", sa.Integer(), nullable=True),
            schema=f"tenant_{slug}",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.drop_column("stock_movements", "user_id", schema=f"tenant_{slug}")
