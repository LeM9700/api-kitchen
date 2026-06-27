"""promotions business interfaces and campaign targeting

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def _add_constraint_if_missing(bind, schema: str, table: str, name: str, ddl: str) -> None:
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE constraint_schema = :schema
                      AND table_name = :table
                      AND constraint_name = :name
                ) THEN
                    ALTER TABLE "{schema}".{table} ADD CONSTRAINT {name} {ddl};
                END IF;
            END $$;
            """
        ),
        {"schema": schema, "table": table, "name": name},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.promotion_campaigns (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    prefix VARCHAR(32) NOT NULL,
                    description VARCHAR(255),
                    created_by_user_id INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )

        bind.execute(sa.text(f"ALTER TABLE {quoted}.promotions ADD COLUMN IF NOT EXISTS campaign_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.promotions ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.promotions ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.promotions ADD COLUMN IF NOT EXISTS is_stackable BOOLEAN NOT NULL DEFAULT FALSE"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.promotions ADD COLUMN IF NOT EXISTS email_verified_required BOOLEAN NOT NULL DEFAULT FALSE"))

        _add_constraint_if_missing(
            bind,
            schema,
            "promotions",
            "fk_promotions_campaign_id",
            f'FOREIGN KEY (campaign_id) REFERENCES "{schema}".promotion_campaigns(id) ON DELETE SET NULL',
        )

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.promotion_target_categories (
                    id SERIAL PRIMARY KEY,
                    promotion_id INTEGER NOT NULL REFERENCES {quoted}.promotions(id) ON DELETE CASCADE,
                    category_id INTEGER NOT NULL REFERENCES {quoted}.categories(id) ON DELETE CASCADE,
                    CONSTRAINT uq_promo_target_category UNIQUE (promotion_id, category_id)
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.promotion_target_products (
                    id SERIAL PRIMARY KEY,
                    promotion_id INTEGER NOT NULL REFERENCES {quoted}.promotions(id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES {quoted}.products(id) ON DELETE CASCADE,
                    CONSTRAINT uq_promo_target_product UNIQUE (promotion_id, product_id)
                )
                """
            )
        )

        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promotions_code_{slug} ON {quoted}.promotions (code)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promotions_campaign_id_{slug} ON {quoted}.promotions (campaign_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promotions_user_id_{slug} ON {quoted}.promotions (user_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promotions_active_dates_{slug} ON {quoted}.promotions (is_active, starts_at, ends_at)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promo_target_categories_promo_{slug} ON {quoted}.promotion_target_categories (promotion_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promo_target_categories_category_{slug} ON {quoted}.promotion_target_categories (category_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promo_target_products_promo_{slug} ON {quoted}.promotion_target_products (promotion_id)"))
        bind.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_promo_target_products_product_{slug} ON {quoted}.promotion_target_products (product_id)"))


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(f"ix_promo_target_products_product_{slug}", table_name="promotion_target_products", schema=schema)
        op.drop_index(f"ix_promo_target_products_promo_{slug}", table_name="promotion_target_products", schema=schema)
        op.drop_index(f"ix_promo_target_categories_category_{slug}", table_name="promotion_target_categories", schema=schema)
        op.drop_index(f"ix_promo_target_categories_promo_{slug}", table_name="promotion_target_categories", schema=schema)
        op.drop_index(f"ix_promotions_active_dates_{slug}", table_name="promotions", schema=schema)
        op.drop_index(f"ix_promotions_user_id_{slug}", table_name="promotions", schema=schema)
        op.drop_index(f"ix_promotions_campaign_id_{slug}", table_name="promotions", schema=schema)
        op.drop_index(f"ix_promotions_code_{slug}", table_name="promotions", schema=schema)
        op.drop_table("promotion_target_products", schema=schema)
        op.drop_table("promotion_target_categories", schema=schema)
        op.drop_constraint("fk_promotions_campaign_id", "promotions", schema=schema, type_="foreignkey")
        op.drop_column("promotions", "email_verified_required", schema=schema)
        op.drop_column("promotions", "is_stackable", schema=schema)
        op.drop_column("promotions", "is_public", schema=schema)
        op.drop_column("promotions", "user_id", schema=schema)
        op.drop_column("promotions", "campaign_id", schema=schema)
        op.drop_table("promotion_campaigns", schema=schema)
