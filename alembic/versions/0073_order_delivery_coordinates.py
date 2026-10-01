"""Add delivery coordinates and instructions to orders

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op


revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("delivery_lat", sa.Float()),
    ("delivery_lng", sa.Float()),
    ("delivery_instructions", sa.Text()),
)


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


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


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "orders"):
            continue
        for name, column_type in _COLUMNS:
            if _column_exists(bind, schema, "orders", name):
                continue
            op.add_column("orders", sa.Column(name, column_type, nullable=True), schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for name, _column_type in _COLUMNS:
            if _column_exists(bind, schema, "orders", name):
                op.drop_column("orders", name, schema=schema)
