"""Ajoute les colonnes de suspension sur public.tenants.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default="false"),
        schema="public",
    )
    op.add_column(
        "tenants",
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "tenants",
        sa.Column("suspension_message", sa.Text(), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("tenants", "suspension_message", schema="public")
    op.drop_column("tenants", "suspended_at", schema="public")
    op.drop_column("tenants", "is_suspended", schema="public")
