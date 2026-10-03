"""Driver GPS: consent columns, sampled location history, last known location

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op


revision = "0078"
down_revision = "0077"
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


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "driver_profiles"):
            continue

        if not _column_exists(bind, schema, "driver_profiles", "location_consent_at"):
            op.add_column(
                "driver_profiles",
                sa.Column("location_consent_at", sa.DateTime(timezone=True), nullable=True),
                schema=schema,
            )
        if not _column_exists(bind, schema, "driver_profiles", "location_consent_version"):
            op.add_column(
                "driver_profiles",
                sa.Column("location_consent_version", sa.String(32), nullable=True),
                schema=schema,
            )

        if not _table_exists(bind, schema, "driver_location_points"):
            op.create_table(
                "driver_location_points",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column(
                    "driver_id", sa.Integer(), sa.ForeignKey(f"{schema}.driver_profiles.id"), nullable=False
                ),
                sa.Column("run_id", sa.Integer(), sa.ForeignKey(f"{schema}.delivery_runs.id"), nullable=True),
                sa.Column("lat", sa.Float(), nullable=False),
                sa.Column("lng", sa.Float(), nullable=False),
                sa.Column("accuracy_m", sa.Float(), nullable=True),
                sa.Column("speed_mps", sa.Float(), nullable=True),
                sa.Column("heading", sa.Float(), nullable=True),
                sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                schema=schema,
            )
            op.create_index(
                "ix_driver_location_points_driver_id_recorded_at",
                "driver_location_points",
                ["driver_id", "recorded_at"],
                schema=schema,
            )
            op.create_index(
                "ix_driver_location_points_recorded_at", "driver_location_points", ["recorded_at"], schema=schema
            )

        if not _table_exists(bind, schema, "driver_last_locations"):
            op.create_table(
                "driver_last_locations",
                sa.Column(
                    "driver_id",
                    sa.Integer(),
                    sa.ForeignKey(f"{schema}.driver_profiles.id"),
                    primary_key=True,
                ),
                sa.Column("run_id", sa.Integer(), sa.ForeignKey(f"{schema}.delivery_runs.id"), nullable=True),
                sa.Column("lat", sa.Float(), nullable=False),
                sa.Column("lng", sa.Float(), nullable=False),
                sa.Column("accuracy_m", sa.Float(), nullable=True),
                sa.Column("speed_mps", sa.Float(), nullable=True),
                sa.Column("heading", sa.Float(), nullable=True),
                sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
                schema=schema,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for table in ("driver_last_locations", "driver_location_points"):
            if _table_exists(bind, schema, table):
                op.drop_table(table, schema=schema)
        for column in ("location_consent_version", "location_consent_at"):
            if _column_exists(bind, schema, "driver_profiles", column):
                op.drop_column("driver_profiles", column, schema=schema)
