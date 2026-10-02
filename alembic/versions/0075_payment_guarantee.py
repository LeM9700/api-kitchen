"""Payment guarantee (card hold) columns on payments

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op


revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("purpose", sa.String(16), nullable=False, server_default="sale"),
    sa.Column("guarantee_terms_version", sa.String(32), nullable=True),
    sa.Column("guarantee_terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("captured_amount", sa.Numeric(10, 2), nullable=True),
    sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("settled_by_user_id", sa.Integer(), nullable=True),
    sa.Column("settlement_note", sa.String(256), nullable=True),
)


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


def _constraint_exists(bind, schema: str, table_name: str, constraint_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM information_schema.table_constraints
                   WHERE table_schema = :schema
                     AND table_name = :table_name
                     AND constraint_name = :constraint_name
                )"""
            ),
            {"schema": schema, "table_name": table_name, "constraint_name": constraint_name},
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "payments"):
            continue
        for column in _COLUMNS:
            if not _column_exists(bind, schema, "payments", column.name):
                op.add_column("payments", column.copy(), schema=schema)
        if not _constraint_exists(bind, schema, "payments", "ck_payments_purpose"):
            op.create_check_constraint(
                "ck_payments_purpose", "payments", "purpose IN ('sale', 'guarantee')", schema=schema
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "payments"):
            continue
        if _constraint_exists(bind, schema, "payments", "ck_payments_purpose"):
            op.drop_constraint("ck_payments_purpose", "payments", schema=schema, type_="check")
        for column in reversed(_COLUMNS):
            if _column_exists(bind, schema, "payments", column.name):
                op.drop_column("payments", column.name, schema=schema)
