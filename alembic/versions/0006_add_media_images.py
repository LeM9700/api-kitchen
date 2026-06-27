"""add media_images table to tenant schemas

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    """Récupère tous les slugs de tenants existants depuis le schema public.

    Args:
        bind: Connexion SQLAlchemy active (``op.get_bind()``).

    Returns:
        Liste des slugs de tenants.
    """
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Crée la table ``media_images`` dans chaque schema tenant existant.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure la table
    dans leur script de provisioning.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.create_table(
            "media_images",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("entity_type", sa.String(32), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("cloudinary_public_id", sa.String(256), nullable=False, unique=True),
            sa.Column("url", sa.String(512), nullable=False),
            sa.Column("url_thumbnail", sa.String(512), nullable=False),
            sa.Column("url_medium", sa.String(512), nullable=False),
            sa.Column("format", sa.String(10), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("alt_text", sa.String(256), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )
        op.create_index(
            "ix_media_images_entity",
            "media_images",
            ["entity_type", "entity_id"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table ``media_images`` de chaque schema tenant existant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index("ix_media_images_entity", table_name="media_images", schema=schema)
        op.drop_table("media_images", schema=schema)
