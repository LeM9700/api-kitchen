"""Add public.super_admins table for dedicated super-admin authentication

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.super_admins (
            id          SERIAL PRIMARY KEY,
            email       VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_login_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_admins_email ON public.super_admins (email)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.super_admins CASCADE")
