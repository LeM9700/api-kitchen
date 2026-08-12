"""Add catalog_snapshots and product_overrides tables (per-tenant)

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-11

Synchronisation catalogue POS (hub) en cache local — voir
docs/superpowers/specs/2026-08-11-hub-catalog-sync-design.md.

Schema tenant (donnee catalogue, comme products) -- pas schema public (a la
difference de pos_connections, qui est une metadonnee d'infrastructure).
Boucle sur public.tenants, meme pattern que 0026.

Nouveaux tenants : ces deux tables sont deja creees automatiquement via
Base.metadata.create_all() dans app/modules/auth/service.py::_provision_tenant_schema
(app/modules/catalog/models.py est importe par app.core.database.tenant_models) --
cette migration ne concerne que les tenants deja provisionnes avant ce lot.
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
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
            "catalog_snapshots",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("connection_id", sa.Integer, nullable=False, unique=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("normalized", sa.JSON(), nullable=False),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            schema=schema,
        )
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


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_table("product_overrides", schema=schema)
        op.drop_table("catalog_snapshots", schema=schema)
