"""Sécurité fidélité et promos : contraintes multiplier + unicité promo_code_usages.

Ajoute :
- loyalty_rules : CHECK multiplier [1.0, 10.0] et priority [0, 100]
- promo_code_usages : UNIQUE (promo_code_id, order_id)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    """Récupère tous les slugs de tenants existants depuis le schéma public.

    Args:
        bind: Connexion SQLAlchemy active (``op.get_bind()``).

    Returns:
        Liste des slugs de tenants.
    """
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Ajoute les contraintes de sécurité sur loyalty_rules et promo_code_usages."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # ------------------------------------------------------------------
        # loyalty_rules : bornes multiplier et priority
        # [🔒 SÉCURITÉ] Dernière ligne de défense même si la validation Pydantic
        # est contournée (ex : migration directe en DB ou script admin).
        # ------------------------------------------------------------------
        op.create_check_constraint(
            f"ck_loyalty_rule_multiplier_range_{slug}",
            "loyalty_rules",
            "multiplier >= 1.0 AND multiplier <= 10.0",
            schema=schema,
        )
        op.create_check_constraint(
            f"ck_loyalty_rule_priority_range_{slug}",
            "loyalty_rules",
            "priority >= 0 AND priority <= 100",
            schema=schema,
        )

        # ------------------------------------------------------------------
        # promo_code_usages : unicité (promo, commande)
        # [🔒 SÉCURITÉ] Empêche le double-enregistrement d'une même commande
        # sous le même code promo — résilience aux retries dans orders/service.py.
        # ------------------------------------------------------------------
        op.create_unique_constraint(
            f"uq_promo_usage_code_order_{slug}",
            "promo_code_usages",
            ["promo_code_id", "order_id"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime les contraintes ajoutées par cette migration."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_constraint(
            f"uq_promo_usage_code_order_{slug}",
            "promo_code_usages",
            schema=schema,
            type_="unique",
        )
        op.drop_constraint(
            f"ck_loyalty_rule_priority_range_{slug}",
            "loyalty_rules",
            schema=schema,
            type_="check",
        )
        op.drop_constraint(
            f"ck_loyalty_rule_multiplier_range_{slug}",
            "loyalty_rules",
            schema=schema,
            type_="check",
        )
