"""Add authority to order_status_history (per schema tenant)

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-04

Ajoute la colonne authority ('internal' | 'external') sur order_status_history,
dans chaque schema tenant. Defaut 'internal' pour retrocompatibilite -- toutes
les transitions historiques ont ete imposees par ce systeme lui-meme.

VARCHAR + CHECK constraint plutot qu'un type ENUM Postgres natif, pour rester
coherent avec le pattern deja en place sur ce module (status, payment_status,
order_type... = VARCHAR valide cote application/Pydantic), cf.
0034_add_order_type_to_orders pour le meme pattern.
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
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
            "order_status_history",
            sa.Column("authority", sa.String(16), nullable=False, server_default="internal"),
            schema=schema,
        )
        op.create_check_constraint(
            f"ck_order_status_history_authority_{slug}",
            "order_status_history",
            "authority IN ('internal', 'external')",
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_constraint(
            f"ck_order_status_history_authority_{slug}", "order_status_history", schema=schema, type_="check"
        )
        op.drop_column("order_status_history", "authority", schema=schema)
