"""add email verification columns to users and promo_code to orders

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    # --- public.users : colonnes email verification ---
    op.add_column("users", sa.Column("email_verification_token", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("email_verification_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))

    # --- tenant_{slug}.orders : promo_code ---
    # [⚠️ PROD] Itération sur tous les tenants existants au moment de la migration.
    # Les tenants créés après cette migration reçoivent la colonne via la procédure
    # de provisioning du schema tenant (CREATE SCHEMA + apply_tenant_migrations).
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.add_column(
            "orders",
            sa.Column("promo_code", sa.String(64), nullable=True),
            schema=f"tenant_{slug}",
        )


def downgrade() -> None:
    # --- public.users ---
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verification_expires_at")
    op.drop_column("users", "email_verification_token")

    # --- tenant_{slug}.orders ---
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.drop_column("orders", "promo_code", schema=f"tenant_{slug}")
