"""Delivery proof code, code attempts, delivery failures, proof/failure settings

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op


revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    return [row[0] for row in bind.execute(sa.text("SELECT slug FROM public.tenants"))]


def _table_exists(bind, schema: str, table_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM information_schema.tables
                   WHERE table_schema = :schema AND table_name = :table_name
                )"""
            ),
            {"schema": schema, "table_name": table_name},
        ).scalar()
    )


def _column_exists(bind, schema: str, table_name: str, column_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema = :schema
                     AND table_name = :table_name
                     AND column_name = :column_name
                )"""
            ),
            {"schema": schema, "table_name": table_name, "column_name": column_name},
        ).scalar()
    )


def _now():
    return sa.text("now()")


_SETTINGS_COLUMNS = (
    ("delivery_proof_required", lambda: sa.Column("delivery_proof_required", sa.Boolean(), nullable=False, server_default="false")),
    ("failure_min_wait_minutes", lambda: sa.Column("failure_min_wait_minutes", sa.Integer(), nullable=False, server_default="5")),
    ("failure_min_call_attempts", lambda: sa.Column("failure_min_call_attempts", sa.Integer(), nullable=False, server_default="1")),
)


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "deliveries"):
            continue

        if not _column_exists(bind, schema, "orders", "delivery_code_nonce"):
            op.add_column(
                "orders", sa.Column("delivery_code_nonce", sa.String(32), nullable=True), schema=schema
            )

        if _table_exists(bind, schema, "restaurant_delivery_settings"):
            for name, make in _SETTINGS_COLUMNS:
                if not _column_exists(bind, schema, "restaurant_delivery_settings", name):
                    op.add_column("restaurant_delivery_settings", make(), schema=schema)

        if not _table_exists(bind, schema, "delivery_code_attempts"):
            op.create_table(
                "delivery_code_attempts",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column(
                    "delivery_id", sa.Integer(), sa.ForeignKey(f"{schema}.deliveries.id"), nullable=False
                ),
                sa.Column("user_id", sa.Integer(), nullable=True),
                sa.Column("success", sa.Boolean(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                schema=schema,
            )
            op.create_index(
                "ix_delivery_code_attempts_delivery_id", "delivery_code_attempts", ["delivery_id"], schema=schema
            )

        if not _table_exists(bind, schema, "delivery_failures"):
            op.create_table(
                "delivery_failures",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column(
                    "delivery_id", sa.Integer(), sa.ForeignKey(f"{schema}.deliveries.id"), nullable=False
                ),
                sa.Column("order_id", sa.Integer(), nullable=False),
                sa.Column("driver_id", sa.Integer(), nullable=False),
                sa.Column("reason", sa.String(32), nullable=False),
                sa.Column("fault", sa.String(16), nullable=False),
                sa.Column("note", sa.String(256), nullable=True),
                sa.Column("call_attempts", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("waited_seconds", sa.Integer(), nullable=True),
                sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
                sa.Column("resolution", sa.String(16), nullable=True),
                sa.Column("retained_amount", sa.Numeric(10, 2), nullable=True),
                sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
                sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("resolution_note", sa.String(256), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.CheckConstraint("status IN ('pending', 'resolved')", name="ck_delivery_failures_status"),
                sa.CheckConstraint("fault IN ('customer', 'restaurant')", name="ck_delivery_failures_fault"),
                schema=schema,
            )
            op.create_index("ix_delivery_failures_status", "delivery_failures", ["status"], schema=schema)
            op.create_index("ix_delivery_failures_order_id", "delivery_failures", ["order_id"], schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for table in ("delivery_failures", "delivery_code_attempts"):
            if _table_exists(bind, schema, table):
                op.drop_table(table, schema=schema)
        for name, _make in _SETTINGS_COLUMNS:
            if _column_exists(bind, schema, "restaurant_delivery_settings", name):
                op.drop_column("restaurant_delivery_settings", name, schema=schema)
        if _column_exists(bind, schema, "orders", "delivery_code_nonce"):
            op.drop_column("orders", "delivery_code_nonce", schema=schema)
