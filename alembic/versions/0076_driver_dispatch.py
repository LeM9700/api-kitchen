"""Driver profiles, delivery runs, deliveries, delivery events, dispatch flag

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op


revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('assigned', 'out_for_delivery', 'arrived')"


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


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "establishments") or not _table_exists(bind, schema, "orders"):
            continue

        if not _table_exists(bind, schema, "driver_profiles"):
            op.create_table(
                "driver_profiles",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("user_id", sa.Integer(), nullable=False, unique=True),
                sa.Column(
                    "establishment_id",
                    sa.Integer(),
                    sa.ForeignKey(f"{schema}.establishments.id"),
                    nullable=False,
                ),
                sa.Column("phone", sa.String(32), nullable=True),
                sa.Column("vehicle", sa.String(64), nullable=True),
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                schema=schema,
            )
            op.create_index(
                "ix_driver_profiles_establishment_id", "driver_profiles", ["establishment_id"], schema=schema
            )

        if not _table_exists(bind, schema, "delivery_runs"):
            op.create_table(
                "delivery_runs",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column(
                    "driver_id", sa.Integer(), sa.ForeignKey(f"{schema}.driver_profiles.id"), nullable=False
                ),
                sa.Column("status", sa.String(16), nullable=False, server_default="active"),
                sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
                sa.CheckConstraint("status IN ('active', 'completed')", name="ck_delivery_runs_status"),
                schema=schema,
            )
            op.create_index(
                "ix_delivery_runs_driver_id_status", "delivery_runs", ["driver_id", "status"], schema=schema
            )

        if not _table_exists(bind, schema, "deliveries"):
            op.create_table(
                "deliveries",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("order_id", sa.Integer(), sa.ForeignKey(f"{schema}.orders.id"), nullable=False),
                sa.Column(
                    "driver_id", sa.Integer(), sa.ForeignKey(f"{schema}.driver_profiles.id"), nullable=False
                ),
                sa.Column("run_id", sa.Integer(), sa.ForeignKey(f"{schema}.delivery_runs.id"), nullable=True),
                sa.Column("status", sa.String(24), nullable=False, server_default="assigned"),
                sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.Column("assigned_by_user_id", sa.Integer(), nullable=True),
                sa.Column("departed_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                sa.CheckConstraint(
                    "status IN ('assigned', 'out_for_delivery', 'arrived', 'delivered', 'failed', 'cancelled')",
                    name="ck_deliveries_status",
                ),
                schema=schema,
            )
            op.create_index(
                "ix_deliveries_driver_id_status", "deliveries", ["driver_id", "status"], schema=schema
            )
            op.create_index("ix_deliveries_order_id", "deliveries", ["order_id"], schema=schema)
            op.create_index(
                "uq_deliveries_one_active_per_order",
                "deliveries",
                ["order_id"],
                unique=True,
                postgresql_where=sa.text(_ACTIVE),
                schema=schema,
            )

        if not _table_exists(bind, schema, "delivery_events"):
            op.create_table(
                "delivery_events",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column(
                    "delivery_id", sa.Integer(), sa.ForeignKey(f"{schema}.deliveries.id"), nullable=True
                ),
                sa.Column("order_id", sa.Integer(), nullable=False),
                sa.Column("driver_id", sa.Integer(), nullable=True),
                sa.Column("event", sa.String(32), nullable=False),
                sa.Column("actor_user_id", sa.Integer(), nullable=True),
                sa.Column("note", sa.String(256), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                schema=schema,
            )
            op.create_index("ix_delivery_events_order_id", "delivery_events", ["order_id"], schema=schema)

        if _table_exists(bind, schema, "restaurant_delivery_settings") and not _column_exists(
            bind, schema, "restaurant_delivery_settings", "driver_dispatch_enabled"
        ):
            op.add_column(
                "restaurant_delivery_settings",
                sa.Column("driver_dispatch_enabled", sa.Boolean(), nullable=False, server_default="false"),
                schema=schema,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if _column_exists(bind, schema, "restaurant_delivery_settings", "driver_dispatch_enabled"):
            op.drop_column("restaurant_delivery_settings", "driver_dispatch_enabled", schema=schema)
        for table in ("delivery_events", "deliveries", "delivery_runs", "driver_profiles"):
            if _table_exists(bind, schema, table):
                op.drop_table(table, schema=schema)
