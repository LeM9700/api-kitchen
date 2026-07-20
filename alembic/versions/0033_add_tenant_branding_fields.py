"""Add branding fields to tenant_config per tenant schema

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-20

Champs ajoutés sur tenant_config (par schema tenant) :
  - display_name  VARCHAR(120)  -- nom d'affichage public du restaurant
  - logo_url      TEXT          -- URL complète (Cloudinary recommandé)
  - primary_color VARCHAR(7)    -- ex: "#E63946" (format #RRGGBB)
  - secondary_color VARCHAR(7)
  - font_family   VARCHAR(50)   -- enum : inter | poppins | playfair_display
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
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
            sa.Column("display_name", sa.String(120), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("logo_url", sa.Text(), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("primary_color", sa.String(7), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("secondary_color", sa.String(7), nullable=True),
            schema=schema,
        )
        op.add_column(
            "tenant_config",
            sa.Column("font_family", sa.String(50), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for col in ("font_family", "secondary_color", "primary_color", "logo_url", "display_name"):
            op.drop_column("tenant_config", col, schema=schema)
