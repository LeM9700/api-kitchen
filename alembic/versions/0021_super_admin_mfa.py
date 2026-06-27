"""Ajoute les colonnes MFA sur users.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column("users", sa.Column("mfa_secret", sa.String(255), nullable=True), schema=schema)
        op.add_column(
            "users",
            sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            schema=schema,
        )
        op.add_column("users", sa.Column("mfa_backup_codes", sa.JSON(), nullable=True), schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("users", "mfa_backup_codes", schema=schema)
        op.drop_column("users", "mfa_enabled", schema=schema)
        op.drop_column("users", "mfa_secret", schema=schema)
