"""Ajout de la table allergen_change_audits (audit trail légal allergènes)

Revision ID: 0013
Revises: 0009
Create Date: 2026-06-22

Table créée (par schema tenant) :
- allergen_change_audits : historique immuable de tout changement de niveau
  d'allergène sur un produit (source manuelle ou recalcul automatique).

Index :
- (product_id, changed_at) DESC — accès O(log n) pour l'endpoint audit admin.

[🔒 SÉCURITÉ LÉGALE] Règlement UE n°1169/2011 — la traçabilité des modifications
d'informations allergènes est une obligation réglementaire en restauration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
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
    """Crée la table allergen_change_audits dans chaque schema tenant.

    [⚠️ PROD] Itère sur tous les tenants existants via public.tenants.
    Les nouveaux tenants créés après cette migration doivent inclure cette
    table dans leur script de provisioning.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.create_table(
            "allergen_change_audits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("allergen_id", sa.Integer(), nullable=False),
            sa.Column("changed_by_user_id", sa.Integer(), nullable=False),
            sa.Column(
                "changed_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("old_level", sa.String(10), nullable=True),
            sa.Column("new_level", sa.String(10), nullable=False),
            sa.Column("old_source", sa.String(10), nullable=True),
            sa.Column("new_source", sa.String(10), nullable=False),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            schema=schema,
        )

        op.create_index(
            f"ix_allergen_change_audits_product_changed_{slug}",
            "allergen_change_audits",
            ["product_id", "changed_at"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table allergen_change_audits de chaque schema tenant.

    [⚠️ PROD] Opération irréversible — toutes les données d'audit seront perdues.
    À n'exécuter qu'en environnement de développement.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_index(
            f"ix_allergen_change_audits_product_changed_{slug}",
            table_name="allergen_change_audits",
            schema=schema,
        )
        op.drop_table("allergen_change_audits", schema=schema)
