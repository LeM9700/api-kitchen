"""Stock recipe constraints and ingredient costs

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def _dedupe_recipe_table(bind, schema: str, table: str, target_column: str) -> None:
    bind.execute(
        sa.text(
            f"""
            DELETE FROM "{schema}".{table}
            WHERE quantity <= 0
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            WITH grouped AS (
                SELECT
                    MIN(id) AS keep_id,
                    {target_column} AS target_id,
                    ingredient_id,
                    SUM(quantity) AS total_quantity
                FROM "{schema}".{table}
                GROUP BY {target_column}, ingredient_id
                HAVING COUNT(*) > 1
            )
            UPDATE "{schema}".{table} t
            SET quantity = grouped.total_quantity
            FROM grouped
            WHERE t.id = grouped.keep_id
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            WITH grouped AS (
                SELECT
                    MIN(id) AS keep_id,
                    {target_column} AS target_id,
                    ingredient_id
                FROM "{schema}".{table}
                GROUP BY {target_column}, ingredient_id
                HAVING COUNT(*) > 1
            )
            DELETE FROM "{schema}".{table} t
            USING grouped
            WHERE t.{target_column} = grouped.target_id
              AND t.ingredient_id = grouped.ingredient_id
              AND t.id <> grouped.keep_id
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE "{schema}".{table} recipe
            SET quantity_unit = ingredient.unit
            FROM "{schema}".ingredients ingredient
            WHERE recipe.ingredient_id = ingredient.id
              AND recipe.quantity_unit IS NULL
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.add_column(
            "ingredients",
            sa.Column("purchase_price_per_unit", sa.Numeric(12, 4), nullable=True),
            schema=schema,
        )
        op.add_column(
            "ingredients",
            sa.Column("purchase_unit", sa.String(32), nullable=True),
            schema=schema,
        )
        op.add_column(
            "product_ingredients",
            sa.Column("quantity_unit", sa.String(32), nullable=True),
            schema=schema,
        )
        op.add_column(
            "variant_ingredients",
            sa.Column("quantity_unit", sa.String(32), nullable=True),
            schema=schema,
        )
        op.add_column(
            "extra_ingredients",
            sa.Column("quantity_unit", sa.String(32), nullable=True),
            schema=schema,
        )

        bind.execute(
            sa.text(
                f"""
                UPDATE "{schema}".ingredients
                SET purchase_unit = unit
                WHERE purchase_unit IS NULL
                """
            )
        )

        _dedupe_recipe_table(bind, schema, "product_ingredients", "product_id")
        _dedupe_recipe_table(bind, schema, "variant_ingredients", "variant_id")
        _dedupe_recipe_table(bind, schema, "extra_ingredients", "extra_id")

        op.create_check_constraint(
            "ck_ingredients_purchase_price_non_negative",
            "ingredients",
            "purchase_price_per_unit IS NULL OR purchase_price_per_unit >= 0",
            schema=schema,
        )
        op.create_check_constraint(
            "ck_product_ingredients_quantity_positive",
            "product_ingredients",
            "quantity > 0",
            schema=schema,
        )
        op.create_check_constraint(
            "ck_variant_ingredients_quantity_positive",
            "variant_ingredients",
            "quantity > 0",
            schema=schema,
        )
        op.create_check_constraint(
            "ck_extra_ingredients_quantity_positive",
            "extra_ingredients",
            "quantity > 0",
            schema=schema,
        )
        op.create_unique_constraint(
            "uq_product_ingredients_product_ingredient",
            "product_ingredients",
            ["product_id", "ingredient_id"],
            schema=schema,
        )
        op.create_unique_constraint(
            "uq_variant_ingredients_variant_ingredient",
            "variant_ingredients",
            ["variant_id", "ingredient_id"],
            schema=schema,
        )
        op.create_unique_constraint(
            "uq_extra_ingredients_extra_ingredient",
            "extra_ingredients",
            ["extra_id", "ingredient_id"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_constraint(
            "uq_extra_ingredients_extra_ingredient",
            "extra_ingredients",
            schema=schema,
            type_="unique",
        )
        op.drop_constraint(
            "uq_variant_ingredients_variant_ingredient",
            "variant_ingredients",
            schema=schema,
            type_="unique",
        )
        op.drop_constraint(
            "uq_product_ingredients_product_ingredient",
            "product_ingredients",
            schema=schema,
            type_="unique",
        )
        op.drop_constraint(
            "ck_extra_ingredients_quantity_positive",
            "extra_ingredients",
            schema=schema,
            type_="check",
        )
        op.drop_constraint(
            "ck_variant_ingredients_quantity_positive",
            "variant_ingredients",
            schema=schema,
            type_="check",
        )
        op.drop_constraint(
            "ck_product_ingredients_quantity_positive",
            "product_ingredients",
            schema=schema,
            type_="check",
        )
        op.drop_constraint(
            "ck_ingredients_purchase_price_non_negative",
            "ingredients",
            schema=schema,
            type_="check",
        )

        op.drop_column("extra_ingredients", "quantity_unit", schema=schema)
        op.drop_column("variant_ingredients", "quantity_unit", schema=schema)
        op.drop_column("product_ingredients", "quantity_unit", schema=schema)
        op.drop_column("ingredients", "purchase_unit", schema=schema)
        op.drop_column("ingredients", "purchase_price_per_unit", schema=schema)
