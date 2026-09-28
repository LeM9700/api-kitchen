"""Add KDS screen remote enabled flag

Revision ID: 0072
Revises: 0071
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


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
        if not _table_exists(bind, schema, "kds_screens"):
            continue
        if _column_exists(bind, schema, "kds_screens", "remote_enabled"):
            continue
        op.add_column(
            "kds_screens",
            sa.Column(
                "remote_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if _column_exists(bind, schema, "kds_screens", "remote_enabled"):
            op.drop_column("kds_screens", "remote_enabled", schema=schema)
