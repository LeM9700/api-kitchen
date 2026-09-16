"""Add public contact fields to tenant_config

Revision ID: 0061
Revises: 0060
Create Date: 2026-09-16

Ces champs alimentent les apps publiques et admin pour afficher les contacts
du restaurant sans exposer la configuration tenant complete.
"""

from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
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
            sa.Column("contact_phone", sa.String(32), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("contact_email", sa.String(255), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("instagram_url", sa.Text(), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("google_business_url", sa.Text(), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "google_business_url", schema=schema)
        op.drop_column("tenant_config", "instagram_url", schema=schema)
        op.drop_column("tenant_config", "contact_email", schema=schema)
        op.drop_column("tenant_config", "contact_phone", schema=schema)
