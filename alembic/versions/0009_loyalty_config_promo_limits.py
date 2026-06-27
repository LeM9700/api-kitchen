"""Fidélité configurable et limites d'usage des promotions.

Crée :
- loyalty_config (configuration globale du programme de fidélité par tenant)
- loyalty_rules (règles de bonus de points configurables)
- loyalty_rewards (paliers de récompenses échangeables)
- promo_code_usages (traçabilité des utilisations de codes promo)

Modifie :
- promotions : ajout de max_uses, max_uses_per_user, current_uses, first_order_only

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0009c"
down_revision = "0009b"
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
    """Crée les tables de fidélité configurable et ajoute les colonnes de limite promo.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure ces tables
    dans leur script de provisioning.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # ------------------------------------------------------------------
        # loyalty_config — configuration globale (ligne unique par tenant)
        # ------------------------------------------------------------------
        op.create_table(
            "loyalty_config",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "base_ratio",
                sa.Numeric(10, 4),
                nullable=False,
                server_default=sa.text("1.0"),
                comment="Points par euro dépensé",
            ),
            sa.Column("points_expiry_days", sa.Integer(), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )

        # ------------------------------------------------------------------
        # loyalty_rules — règles de bonus configurables
        # ------------------------------------------------------------------
        op.create_table(
            "loyalty_rules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("rule_type", sa.String(32), nullable=False),
            sa.Column(
                "category_id",
                sa.Integer(),
                sa.ForeignKey(f"{schema}.categories.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("multiplier", sa.Numeric(6, 4), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("days_of_week", sa.ARRAY(sa.Integer()), nullable=True),
            sa.Column(
                "priority",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(
                "rule_type IN ('category_multiplier', 'period_multiplier', 'day_multiplier', 'first_order')",
                name=f"ck_loyalty_rules_rule_type_{slug}",
            ),
            schema=schema,
        )

        # ------------------------------------------------------------------
        # loyalty_rewards — paliers de récompenses
        # ------------------------------------------------------------------
        op.create_table(
            "loyalty_rewards",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("reward_type", sa.String(32), nullable=False),
            sa.Column("points_required", sa.Integer(), nullable=False),
            sa.Column("discount_amount", sa.Numeric(10, 2), nullable=True),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey(f"{schema}.products.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(
                "reward_type IN ('discount_euros', 'free_product')",
                name=f"ck_loyalty_rewards_reward_type_{slug}",
            ),
            schema=schema,
        )
        op.create_index(
            f"ix_loyalty_rewards_points_required_{slug}",
            "loyalty_rewards",
            ["points_required"],
            schema=schema,
        )

        # ------------------------------------------------------------------
        # promotions : nouvelles colonnes de limite d'usage
        # ------------------------------------------------------------------
        op.add_column(
            "promotions",
            sa.Column("max_uses", sa.Integer(), nullable=True),
            schema=schema,
        )
        op.add_column(
            "promotions",
            sa.Column("max_uses_per_user", sa.Integer(), nullable=True),
            schema=schema,
        )
        op.add_column(
            "promotions",
            sa.Column(
                "current_uses",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            schema=schema,
        )
        op.add_column(
            "promotions",
            sa.Column(
                "first_order_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            schema=schema,
        )

        # ------------------------------------------------------------------
        # promo_code_usages — traçabilité des utilisations
        # ------------------------------------------------------------------
        op.create_table(
            "promo_code_usages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "promo_code_id",
                sa.Integer(),
                sa.ForeignKey(f"{schema}.promotions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column(
                "used_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )
        op.create_index(
            f"ix_promo_code_usages_promo_user_{slug}",
            "promo_code_usages",
            ["promo_code_id", "user_id"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime toutes les tables et colonnes créées par cette migration."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # promo_code_usages
        op.drop_index(f"ix_promo_code_usages_promo_user_{slug}", table_name="promo_code_usages", schema=schema)
        op.drop_table("promo_code_usages", schema=schema)

        # promotions — suppression des colonnes ajoutées
        op.drop_column("promotions", "first_order_only", schema=schema)
        op.drop_column("promotions", "current_uses", schema=schema)
        op.drop_column("promotions", "max_uses_per_user", schema=schema)
        op.drop_column("promotions", "max_uses", schema=schema)

        # loyalty_rewards
        op.drop_index(f"ix_loyalty_rewards_points_required_{slug}", table_name="loyalty_rewards", schema=schema)
        op.drop_table("loyalty_rewards", schema=schema)

        # loyalty_rules
        op.drop_table("loyalty_rules", schema=schema)

        # loyalty_config
        op.drop_table("loyalty_config", schema=schema)
