"""Add products.external_product_id/tax_rate; refactor product_overrides to
key on product_id instead of (connection_id, external_product_id)

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-12

Materialisation du catalogue hub dans products -- voir
docs/superpowers/specs/2026-08-12-hub-catalog-materialization-design.md.

product_overrides est recree (drop + create) plutot que migre colonne par
colonne : aucune ligne n'existe en production (aucune API pour en creer
n'a jamais existe avant ce lot), donc aucune donnee a preserver.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.add_column(
            "products",
            sa.Column("external_product_id", sa.String(255), nullable=True),
            schema=schema,
        )
        op.add_column(
            "products",
            sa.Column("tax_rate", sa.Numeric(5, 4), nullable=True),
            schema=schema,
        )
        op.create_unique_constraint(
            "uq_products_external_product_id", "products", ["external_product_id"], schema=schema
        )

        op.drop_table("product_overrides", schema=schema)
        op.create_table(
            "product_overrides",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "product_id",
                sa.Integer,
                sa.ForeignKey(f"{schema}.products.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("image_url", sa.String(512), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_featured", sa.Boolean(), nullable=True),
            sa.Column("display_order", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_table("product_overrides", schema=schema)
        op.create_table(
            "product_overrides",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("connection_id", sa.Integer, nullable=False),
            sa.Column("external_product_id", sa.String(255), nullable=False),
            sa.Column("image_url", sa.String(512), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_featured", sa.Boolean(), nullable=True),
            sa.Column("display_order", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "connection_id", "external_product_id", name="uq_product_overrides_connection_external_id"
            ),
            schema=schema,
        )

        op.drop_constraint("uq_products_external_product_id", "products", schema=schema, type_="unique")
        op.drop_column("products", "tax_rate", schema=schema)
        op.drop_column("products", "external_product_id", schema=schema)
