"""Admin/staff API contracts for manual orders and preparation

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'

        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.orders DROP CONSTRAINT IF EXISTS ck_orders_order_type_{slug}")
        )
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders DROP CONSTRAINT IF EXISTS ck_orders_order_type"))
        op.create_check_constraint(
            f"ck_orders_order_type_{slug}",
            "orders",
            "order_type IN ('delivery', 'pickup', 'dine_in')",
            schema=schema,
        )

        # ADD COLUMN IF NOT EXISTS (au lieu de op.add_column) : les tenants crees via
        # _TENANT_DDL_STATEMENTS (app/modules/auth/service.py) depuis l'ajout de ces
        # colonnes au provisioning ont deja ce schema au moment de leur signup, avant
        # meme que cette migration ne tourne sur les tenants existants. Sans le IF NOT
        # EXISTS, la migration echoue avec DuplicateColumnError des qu'un tel tenant
        # existe (cf. incident prod 2026-08-04).
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(32)"))
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.orders ADD COLUMN IF NOT EXISTS source VARCHAR(16) "
                "NOT NULL DEFAULT 'customer'"
            )
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.orders ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER")
        )
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders ADD COLUMN IF NOT EXISTS table_number VARCHAR(32)"))
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders DROP CONSTRAINT IF EXISTS ck_orders_source_{slug}"))
        op.create_check_constraint(
            f"ck_orders_source_{slug}",
            "orders",
            "source IN ('customer', 'manual', 'system')",
            schema=schema,
        )

        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.order_items ADD COLUMN IF NOT EXISTS preparation_status VARCHAR(16) "
                "NOT NULL DEFAULT 'pending'"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.order_items ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16) "
                "NOT NULL DEFAULT 'kitchen'"
            )
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.order_items ADD COLUMN IF NOT EXISTS prepared_at TIMESTAMPTZ")
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.order_items ADD COLUMN IF NOT EXISTS prepared_by_user_id INTEGER")
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.order_items DROP CONSTRAINT IF EXISTS "
                f"ck_order_items_preparation_status_{slug}"
            )
        )
        op.create_check_constraint(
            f"ck_order_items_preparation_status_{slug}",
            "order_items",
            "preparation_status IN ('pending', 'preparing', 'ready')",
            schema=schema,
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.order_items DROP CONSTRAINT IF EXISTS "
                f"ck_order_items_preparation_station_{slug}"
            )
        )
        op.create_check_constraint(
            f"ck_order_items_preparation_station_{slug}",
            "order_items",
            "preparation_station IN ('kitchen', 'counter', 'none')",
            schema=schema,
        )

        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.categories ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16) "
                "NOT NULL DEFAULT 'kitchen'"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.categories DROP CONSTRAINT IF EXISTS "
                f"ck_categories_preparation_station_{slug}"
            )
        )
        op.create_check_constraint(
            f"ck_categories_preparation_station_{slug}",
            "categories",
            "preparation_station IN ('kitchen', 'counter', 'none')",
            schema=schema,
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.products ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16)")
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted}.products DROP CONSTRAINT IF EXISTS ck_products_preparation_station_{slug}"
            )
        )
        op.create_check_constraint(
            f"ck_products_preparation_station_{slug}",
            "products",
            "preparation_station IS NULL OR preparation_station IN ('kitchen', 'counter', 'none')",
            schema=schema,
        )

        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.payments ADD COLUMN IF NOT EXISTS external_reference VARCHAR(255)")
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.payments ADD COLUMN IF NOT EXISTS amount_received NUMERIC(10, 2)")
        )
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.payments ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER")
        )

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.product_availability_overrides (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES {quoted}.products(id) ON DELETE CASCADE,
                    available BOOLEAN NOT NULL,
                    reason TEXT,
                    changed_by_user_id INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_product_availability_overrides_product_created_{slug} "
                f"ON {quoted}.product_availability_overrides (product_id, created_at)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_table("product_availability_overrides", schema=schema)

        op.drop_column("payments", "created_by_user_id", schema=schema)
        op.drop_column("payments", "amount_received", schema=schema)
        op.drop_column("payments", "external_reference", schema=schema)

        op.drop_constraint(f"ck_products_preparation_station_{slug}", "products", schema=schema, type_="check")
        op.drop_column("products", "preparation_station", schema=schema)
        op.drop_constraint(f"ck_categories_preparation_station_{slug}", "categories", schema=schema, type_="check")
        op.drop_column("categories", "preparation_station", schema=schema)

        op.drop_constraint(
            f"ck_order_items_preparation_station_{slug}",
            "order_items",
            schema=schema,
            type_="check",
        )
        op.drop_constraint(
            f"ck_order_items_preparation_status_{slug}",
            "order_items",
            schema=schema,
            type_="check",
        )
        op.drop_column("order_items", "prepared_by_user_id", schema=schema)
        op.drop_column("order_items", "prepared_at", schema=schema)
        op.drop_column("order_items", "preparation_station", schema=schema)
        op.drop_column("order_items", "preparation_status", schema=schema)

        op.drop_constraint(f"ck_orders_source_{slug}", "orders", schema=schema, type_="check")
        op.drop_column("orders", "table_number", schema=schema)
        op.drop_column("orders", "created_by_user_id", schema=schema)
        op.drop_column("orders", "source", schema=schema)
        op.drop_column("orders", "customer_phone", schema=schema)
        op.drop_column("orders", "customer_name", schema=schema)

        quoted = f'"{schema}"'
        bind.execute(
            sa.text(f"ALTER TABLE {quoted}.orders DROP CONSTRAINT IF EXISTS ck_orders_order_type_{slug}")
        )
        bind.execute(sa.text(f"ALTER TABLE {quoted}.orders DROP CONSTRAINT IF EXISTS ck_orders_order_type"))
        op.create_check_constraint(
            f"ck_orders_order_type_{slug}",
            "orders",
            "order_type IN ('delivery', 'pickup')",
            schema=schema,
        )
