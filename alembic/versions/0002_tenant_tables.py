"""tenant application tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    op.create_table("categories", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(128), nullable=False), sa.Column("display_order", sa.Integer(), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False))
    op.create_table("products", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True), sa.Column("name", sa.String(255), nullable=False), sa.Column("description", sa.Text(), nullable=True), sa.Column("base_price", sa.Numeric(10, 2), nullable=False), sa.Column("image_url", sa.String(512), nullable=True), sa.Column("is_active", sa.Boolean(), nullable=False))
    op.create_table("product_variants", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(128), nullable=False), sa.Column("price_delta", sa.Numeric(10, 2), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False))
    op.create_table("extras", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(128), nullable=False), sa.Column("price", sa.Numeric(10, 2), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False))
    op.create_table("product_extras", sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True), sa.Column("extra_id", sa.Integer(), sa.ForeignKey("extras.id", ondelete="CASCADE"), primary_key=True))

    op.create_table("orders", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), nullable=True), sa.Column("customer_email", sa.String(255), nullable=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("subtotal", sa.Numeric(10, 2), nullable=False), sa.Column("discount_total", sa.Numeric(10, 2), nullable=False), sa.Column("delivery_fee", sa.Numeric(10, 2), nullable=False), sa.Column("total", sa.Numeric(10, 2), nullable=False), sa.Column("delivery_address", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("order_items", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False), sa.Column("product_id", sa.Integer(), nullable=False), sa.Column("variant_id", sa.Integer(), nullable=True), sa.Column("quantity", sa.Integer(), nullable=False), sa.Column("unit_price", sa.Numeric(10, 2), nullable=False), sa.Column("total", sa.Numeric(10, 2), nullable=False))
    op.create_table("order_status_history", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("note", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))

    op.create_table("payments", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False), sa.Column("provider", sa.String(32), nullable=False), sa.Column("provider_payment_id", sa.String(255), nullable=True), sa.Column("amount", sa.Numeric(10, 2), nullable=False), sa.Column("currency", sa.String(8), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_payments_provider_payment_id", "payments", ["provider_payment_id"])

    op.create_table("ingredients", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(128), nullable=False), sa.Column("unit", sa.String(32), nullable=False), sa.Column("current_qty", sa.Numeric(12, 3), nullable=False), sa.Column("alert_threshold", sa.Numeric(12, 3), nullable=False))
    op.create_table("product_ingredients", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("product_id", sa.Integer(), nullable=False), sa.Column("ingredient_id", sa.Integer(), sa.ForeignKey("ingredients.id"), nullable=False), sa.Column("quantity", sa.Numeric(12, 3), nullable=False))
    op.create_table("variant_ingredients", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("variant_id", sa.Integer(), nullable=False), sa.Column("ingredient_id", sa.Integer(), sa.ForeignKey("ingredients.id"), nullable=False), sa.Column("quantity", sa.Numeric(12, 3), nullable=False))
    op.create_table("stock_movements", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("ingredient_id", sa.Integer(), sa.ForeignKey("ingredients.id"), nullable=False), sa.Column("quantity_delta", sa.Numeric(12, 3), nullable=False), sa.Column("reason", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("product_stock", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("product_id", sa.Integer(), nullable=False), sa.Column("available_qty", sa.Integer(), nullable=False))

    op.create_table("loyalty_accounts", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), nullable=False), sa.Column("points", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("user_id"))
    op.create_table("loyalty_transactions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("loyalty_accounts.id", ondelete="CASCADE"), nullable=False), sa.Column("points_delta", sa.Integer(), nullable=False), sa.Column("reason", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))

    op.create_table("promotions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("code", sa.String(64), nullable=False), sa.Column("description", sa.String(255), nullable=True), sa.Column("discount_type", sa.String(16), nullable=False), sa.Column("discount_value", sa.Numeric(10, 2), nullable=False), sa.Column("min_order_amount", sa.Numeric(10, 2), nullable=False), sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True), sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True), sa.Column("is_active", sa.Boolean(), nullable=False), sa.UniqueConstraint("code"))
    op.create_table("delivery_zones", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(128), nullable=False), sa.Column("polygon", sa.JSON(), nullable=False), sa.Column("fee", sa.Numeric(10, 2), nullable=False), sa.Column("min_order_amount", sa.Numeric(10, 2), nullable=False), sa.Column("estimated_minutes", sa.Integer(), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False))


def downgrade() -> None:
    for table in [
        "delivery_zones",
        "promotions",
        "loyalty_transactions",
        "loyalty_accounts",
        "product_stock",
        "stock_movements",
        "variant_ingredients",
        "product_ingredients",
        "ingredients",
        "payments",
        "order_status_history",
        "order_items",
        "orders",
        "product_extras",
        "extras",
        "product_variants",
        "products",
        "categories",
        "refresh_tokens",
        "users",
    ]:
        op.drop_table(table)
