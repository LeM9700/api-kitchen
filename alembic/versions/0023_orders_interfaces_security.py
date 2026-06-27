"""orders interface contracts and security fields

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
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
            sa.Column("payment_status", sa.String(32), nullable=False, server_default="pending"),
            schema=schema,
        )
        op.add_column("orders", sa.Column("delivery_zone_id", sa.Integer(), nullable=True), schema=schema)
        op.add_column(
            "orders",
            sa.Column("estimated_delivery_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
        op.add_column("orders", sa.Column("idempotency_key", sa.String(128), nullable=True), schema=schema)

        op.add_column("order_items", sa.Column("product_name_snapshot", sa.String(255), nullable=True), schema=schema)
        op.add_column("order_items", sa.Column("variant_name_snapshot", sa.String(128), nullable=True), schema=schema)
        op.add_column("order_items", sa.Column("extras_snapshot", sa.JSON(), nullable=True), schema=schema)
        op.add_column(
            "order_items",
            sa.Column("extras_total", sa.Numeric(10, 2), nullable=False, server_default="0"),
            schema=schema,
        )

        op.create_index(f"ix_orders_user_id_{slug}", "orders", ["user_id"], schema=schema)
        op.create_index(f"ix_orders_status_{slug}", "orders", ["status"], schema=schema)
        op.create_index(f"ix_orders_created_at_{slug}", "orders", ["created_at"], schema=schema)
        op.create_unique_constraint(
            f"uq_orders_user_id_idempotency_key_{slug}",
            "orders",
            ["user_id", "idempotency_key"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_constraint(f"uq_orders_user_id_idempotency_key_{slug}", "orders", schema=schema, type_="unique")
        op.drop_index(f"ix_orders_created_at_{slug}", table_name="orders", schema=schema)
        op.drop_index(f"ix_orders_status_{slug}", table_name="orders", schema=schema)
        op.drop_index(f"ix_orders_user_id_{slug}", table_name="orders", schema=schema)

        op.drop_column("order_items", "extras_total", schema=schema)
        op.drop_column("order_items", "extras_snapshot", schema=schema)
        op.drop_column("order_items", "variant_name_snapshot", schema=schema)
        op.drop_column("order_items", "product_name_snapshot", schema=schema)

        op.drop_column("orders", "idempotency_key", schema=schema)
        op.drop_column("orders", "estimated_delivery_at", schema=schema)
        op.drop_column("orders", "delivery_zone_id", schema=schema)
        op.drop_column("orders", "payment_status", schema=schema)
