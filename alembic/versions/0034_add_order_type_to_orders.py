"""Add order_type to orders (per schema tenant)

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-20

Ajoute la colonne order_type ('delivery' | 'pickup') sur orders, dans chaque
schema tenant. Defaut 'delivery' pour retrocompatibilite -- les commandes
existantes n'ont pas de notion de type et sont toutes des livraisons.

VARCHAR + CHECK constraint plutot qu'un type ENUM Postgres natif, pour rester
coherent avec le pattern deja en place sur ce module (status, payment_status,
role... = VARCHAR valide cote application/Pydantic), cf. 0009_allergens_dietary_tags
et 0024_add_non_negative_check_to_ingredients pour le meme pattern CHECK.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
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
            "orders",
            sa.Column("order_type", sa.String(16), nullable=False, server_default="delivery"),
            schema=schema,
        )
        op.create_check_constraint(
            f"ck_orders_order_type_{slug}",
            "orders",
            "order_type IN ('delivery', 'pickup')",
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_constraint(f"ck_orders_order_type_{slug}", "orders", schema=schema, type_="check")
        op.drop_column("orders", "order_type", schema=schema)
