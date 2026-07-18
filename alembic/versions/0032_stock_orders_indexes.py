"""add missing index on stock_movements.ingredient_id and composite orders(status, created_at)

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # [PERF] stock_movements.ingredient_id est une colonne chaude (lookups de
        # disponibilite, historique par ingredient) mais n'a jamais eu d'index
        # explicite au-dela de la contrainte FK (0002_tenant_tables.py).
        op.create_index(
            f"ix_stock_movements_ingredient_id_{slug}",
            "stock_movements",
            ["ingredient_id"],
            schema=schema,
        )

        # [PERF] list_orders filtre par statut ET trie par date simultanement.
        # Les index simples ix_orders_status/ix_orders_created_at (0023) existent
        # deja ; cet index composite est plus efficace que leur combinaison via
        # bitmap scan pour ce pattern de requete precis.
        op.create_index(
            f"ix_orders_status_created_at_{slug}",
            "orders",
            ["status", "created_at"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_index(f"ix_orders_status_created_at_{slug}", table_name="orders", schema=schema)
        op.drop_index(f"ix_stock_movements_ingredient_id_{slug}", table_name="stock_movements", schema=schema)
