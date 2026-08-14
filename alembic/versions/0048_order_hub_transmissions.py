"""Add order_hub_transmissions and processed_hub_order_events tables

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-14

Transmission asynchrone des commandes vers le hub POS -- voir
docs/superpowers/specs/2026-08-13-pos-order-transmission-design.md.
"""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.create_table(
            "order_hub_transmissions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "order_id",
                sa.Integer,
                sa.ForeignKey(f"{schema}.orders.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("hub_order_id", sa.String(128), nullable=True),
            sa.Column("transmission_status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("last_hub_status", sa.String(32), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "transmission_status IN ('pending', 'sent', 'failed', 'acknowledged')",
                name="ck_order_hub_transmissions_status",
            ),
            schema=schema,
        )
        op.create_index(
            "ix_order_hub_transmissions_status",
            "order_hub_transmissions",
            ["transmission_status"],
            schema=schema,
        )

        op.create_table(
            "processed_hub_order_events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("event_id", sa.String(128), nullable=False, unique=True),
            sa.Column("order_id", sa.Integer, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("processed_hub_order_events", schema=schema)
        op.drop_index("ix_order_hub_transmissions_status", table_name="order_hub_transmissions", schema=schema)
        op.drop_table("order_hub_transmissions", schema=schema)
