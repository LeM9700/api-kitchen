"""Attach orders to establishments

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.add_column("orders", sa.Column("establishment_id", sa.Integer(), nullable=True), schema=schema)
        # Historical orders did not store their real establishment. Keep them
        # NULL instead of attributing site-level history to the principal site.
        # New writes resolve a deterministic active establishment in service code.
        op.create_index(
            f"ix_orders_establishment_id_{slug}",
            "orders",
            ["establishment_id"],
            schema=schema,
        )
        op.create_foreign_key(
            f"fk_orders_establishment_{slug}",
            "orders",
            "establishments",
            ["establishment_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_constraint(f"fk_orders_establishment_{slug}", "orders", schema=schema, type_="foreignkey")
        op.drop_index(f"ix_orders_establishment_id_{slug}", table_name="orders", schema=schema)
        op.drop_column("orders", "establishment_id", schema=schema)
