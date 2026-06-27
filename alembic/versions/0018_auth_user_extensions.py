"""Ajoute password_reset_token, password_reset_expires_at et must_change_password sur users (tenant).

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
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
            "users",
            sa.Column("password_reset_token", sa.String(64), nullable=True),
            schema=schema,
        )
        op.add_column(
            "users",
            sa.Column("password_reset_expires_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
        op.add_column(
            "users",
            sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            schema=schema,
        )
        op.create_index(
            "ix_users_password_reset_token",
            "users",
            ["password_reset_token"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index("ix_users_password_reset_token", table_name="users", schema=schema)
        op.drop_column("users", "must_change_password", schema=schema)
        op.drop_column("users", "password_reset_expires_at", schema=schema)
        op.drop_column("users", "password_reset_token", schema=schema)
