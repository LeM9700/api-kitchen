"""Stock recipe audit logs

Revision ID: 0068
Revises: 0067
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
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
            "stock_recipe_audit_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("recipe_type", sa.String(length=16), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column("changed_by_user_id", sa.Integer(), nullable=True),
            sa.Column("old_items", sa.JSON(), nullable=True),
            sa.Column("new_items", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "recipe_type IN ('product', 'variant', 'extra')",
                name="ck_stock_recipe_audit_logs_recipe_type",
            ),
            sa.PrimaryKeyConstraint("id"),
            schema=schema,
        )
        op.create_index(
            "ix_stock_recipe_audit_logs_target",
            "stock_recipe_audit_logs",
            ["recipe_type", "target_id", "created_at"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index("ix_stock_recipe_audit_logs_target", table_name="stock_recipe_audit_logs", schema=schema)
        op.drop_table("stock_recipe_audit_logs", schema=schema)
