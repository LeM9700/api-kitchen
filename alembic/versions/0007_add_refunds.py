"""add refunds table to tenant schemas

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
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
    """Crée la table ``refunds`` dans chaque schema tenant existant.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure la table
    dans leur script de provisioning.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.create_table(
            "refunds",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey(f"{schema}.orders.id"), nullable=False),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey(f"{schema}.payments.id"), nullable=False),
            sa.Column("stripe_refund_id", sa.String(128), nullable=False, unique=True),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(256), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )
        op.create_index(
            f"ix_refunds_order_id_{slug}",
            "refunds",
            ["order_id"],
            schema=schema,
        )
        op.create_index(
            f"ix_refunds_payment_id_{slug}",
            "refunds",
            ["payment_id"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table ``refunds`` de chaque schema tenant existant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(f"ix_refunds_payment_id_{slug}", table_name="refunds", schema=schema)
        op.drop_index(f"ix_refunds_order_id_{slug}", table_name="refunds", schema=schema)
        op.drop_table("refunds", schema=schema)
