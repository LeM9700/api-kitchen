"""Add is_delivery_prohibited to products per tenant schema

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
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
            "products",
            sa.Column("is_delivery_prohibited", sa.Boolean(), nullable=False, server_default=sa.false()),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("products", "is_delivery_prohibited", schema=schema)
