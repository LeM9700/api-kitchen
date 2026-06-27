"""public schema: tenants + tenant_configs

Revision ID: 0001
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False, server_default="starter"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="public",
    )
    op.create_table(
        "tenant_configs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer,
            sa.ForeignKey("public.tenants.id"),
            nullable=False,
        ),
        sa.Column("delivery_zones", sa.JSON, nullable=True),
        sa.Column("stripe_account_id", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(8), server_default="EUR"),
        sa.Column("timezone", sa.String(64), server_default="Europe/Paris"),
        sa.Column("logo_url", sa.String(512), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("tenant_configs", schema="public")
    op.drop_table("tenants", schema="public")
