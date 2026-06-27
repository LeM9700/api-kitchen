"""add non-negative check constraint to ingredients.current_qty

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-24

NOTE: If any tenant already has current_qty < 0 rows, this migration will fail.
Fix before applying: UPDATE tenant_X.ingredients SET current_qty = 0 WHERE current_qty < 0;
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.create_check_constraint(
            f"ck_ingredients_current_qty_non_negative_{slug}",
            "ingredients",
            "current_qty >= 0",
            schema=f"tenant_{slug}",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        op.drop_constraint(
            f"ck_ingredients_current_qty_non_negative_{slug}",
            "ingredients",
            schema=f"tenant_{slug}",
            type_="check",
        )
