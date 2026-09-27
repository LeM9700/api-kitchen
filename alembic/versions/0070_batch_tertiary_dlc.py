"""Add ingredient batch tertiary DLC fields

Revision ID: 0070
Revises: 0069
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa


revision = "0070"
down_revision = "0069"
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
            "ingredient_batches",
            sa.Column("tertiary_started_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
        op.add_column(
            "ingredient_batches",
            sa.Column("tertiary_use_within_hours", sa.Integer(), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("ingredient_batches", "tertiary_use_within_hours", schema=schema)
        op.drop_column("ingredient_batches", "tertiary_started_at", schema=schema)

