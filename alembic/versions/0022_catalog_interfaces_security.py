"""catalog interface contracts, price audit and CSV import

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
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
            "extra_ingredients",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("extra_id", sa.Integer(), sa.ForeignKey(f"{schema}.extras.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), sa.ForeignKey(f"{schema}.ingredients.id"), nullable=False),
            sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
            schema=schema,
        )
        op.create_index(f"ix_extra_ingredients_extra_{slug}", "extra_ingredients", ["extra_id"], schema=schema)
        op.create_index(f"ix_extra_ingredients_ingredient_{slug}", "extra_ingredients", ["ingredient_id"], schema=schema)

        op.create_table(
            "product_recommendations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey(f"{schema}.products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("recommended_product_id", sa.Integer(), sa.ForeignKey(f"{schema}.products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("label", sa.String(128), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.UniqueConstraint("product_id", "recommended_product_id", name=f"uq_product_recommendations_pair_{slug}"),
            schema=schema,
        )
        op.create_index(f"ix_product_recommendations_product_{slug}", "product_recommendations", ["product_id"], schema=schema)
        op.create_index(f"ix_product_recommendations_recommended_{slug}", "product_recommendations", ["recommended_product_id"], schema=schema)

        op.create_table(
            "catalog_price_audits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("entity_type", sa.String(16), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("old_price", sa.Numeric(10, 2), nullable=True),
            sa.Column("new_price", sa.Numeric(10, 2), nullable=False),
            sa.Column("changed_by_user_id", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            schema=schema,
        )
        op.create_index(
            f"ix_catalog_price_audits_entity_{slug}",
            "catalog_price_audits",
            ["entity_type", "entity_id", "changed_at"],
            schema=schema,
        )

        op.create_table(
            "catalog_import_batches",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("token", sa.String(64), nullable=False, unique=True),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("csv_text", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="dry_run"),
            sa.Column("validation_report", sa.JSON(), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("catalog_import_batches", schema=schema)
        op.drop_index(f"ix_catalog_price_audits_entity_{slug}", table_name="catalog_price_audits", schema=schema)
        op.drop_table("catalog_price_audits", schema=schema)
        op.drop_index(f"ix_product_recommendations_recommended_{slug}", table_name="product_recommendations", schema=schema)
        op.drop_index(f"ix_product_recommendations_product_{slug}", table_name="product_recommendations", schema=schema)
        op.drop_table("product_recommendations", schema=schema)
        op.drop_index(f"ix_extra_ingredients_ingredient_{slug}", table_name="extra_ingredients", schema=schema)
        op.drop_index(f"ix_extra_ingredients_extra_{slug}", table_name="extra_ingredients", schema=schema)
        op.drop_table("extra_ingredients", schema=schema)
