"""add tenant_config_audits to tenant schemas

Revision ID: 0012
Revises: 0009
Create Date: 2026-06-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0009c"
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
    """Crée la table ``tenant_config_audits`` dans chaque schema tenant existant.

    [🔒 SÉCURITÉ] Cette table constitue le journal immuable des modifications
    de configuration. Ne pas supprimer les données historiques sans aval légal.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure cette table
    dans leur script de provisioning (``create_tenant_schema``).
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.create_table(
            "tenant_config_audits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            # Référence à public.users.id — pas de FK cross-schema pour éviter
            # les dépendances entre schemas (tenant provisioning indépendant).
            sa.Column("changed_by_user_id", sa.Integer(), nullable=False),
            sa.Column(
                "changed_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("field_name", sa.String(255), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            # [🔒 SÉCURITÉ] IPv6 max = 39 chars, IPv4-mapped IPv6 = 45 chars.
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            schema=schema,
        )
        # [⚡ PERF] Index sur changed_at pour les requêtes "dernières N modifications".
        op.create_index(
            f"ix_tenant_config_audits_changed_at_{slug}",
            "tenant_config_audits",
            ["changed_at"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table ``tenant_config_audits`` de chaque schema tenant existant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_index(
            f"ix_tenant_config_audits_changed_at_{slug}",
            table_name="tenant_config_audits",
            schema=schema,
        )
        op.drop_table("tenant_config_audits", schema=schema)
