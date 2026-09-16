"""Add default_language column to tenant_config

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-15

Contexte : TenantConfig.default_language sert de langue de repli pour les
notifications push et emails clients (voir app/core/i18n/) quand le header
Accept-Language de la requete est absent ou ne correspond pas a une locale
supportee (fr/en). Defaut "fr" pour preserver le comportement actuel (tout
le texte serveur-rendu est en francais) sur les tenants existants.
"""

from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
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
            sa.Column("default_language", sa.String(2), nullable=False, server_default="fr"),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "default_language", schema=schema)
