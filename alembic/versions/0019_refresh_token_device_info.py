"""Ajoute user_agent et ip_address sur refresh_tokens (tenant).

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
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
            "refresh_tokens",
            sa.Column("user_agent", sa.String(512), nullable=True),
            schema=schema,
        )
        op.add_column(
            "refresh_tokens",
            sa.Column("ip_address", sa.String(45), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("refresh_tokens", "ip_address", schema=schema)
        op.drop_column("refresh_tokens", "user_agent", schema=schema)
