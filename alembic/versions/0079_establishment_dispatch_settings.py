"""Per-establishment dispatch settings (mode, driver cap, failure rules)

Revision ID: 0079
Revises: 0078
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op


revision = "0079"
down_revision = "0078"
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


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if not _table_exists(bind, schema, "establishments") or _table_exists(
            bind, schema, "establishment_dispatch_settings"
        ):
            continue
        op.create_table(
            "establishment_dispatch_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "establishment_id",
                sa.Integer(),
                sa.ForeignKey(f"{schema}.establishments.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("dispatch_mode", sa.String(16), nullable=False, server_default="counter"),
            sa.Column("max_active_deliveries", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("failure_min_wait_minutes", sa.Integer(), nullable=True),
            sa.Column("failure_min_call_attempts", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.CheckConstraint("dispatch_mode IN ('counter', 'self_assign')", name="ck_establishment_dispatch_mode"),
            sa.CheckConstraint(
                "max_active_deliveries BETWEEN 1 AND 10", name="ck_establishment_dispatch_max_active"
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if _table_exists(bind, schema, "establishment_dispatch_settings"):
            op.drop_table("establishment_dispatch_settings", schema=schema)
