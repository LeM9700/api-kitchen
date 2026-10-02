"""Delivery zones by establishment, zone pricing rules, establishment location, free-delivery promos

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op


revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


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


def _index_exists(bind, schema: str, index_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :name)"),
            {"schema": schema, "name": index_name},
        ).scalar()
    )


def _add_column_if_missing(bind, schema: str, table: str, column: sa.Column) -> None:
    if not _column_exists(bind, schema, table, column.name):
        op.add_column(table, column, schema=schema)


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # --- establishments : position (centre des cartes de livraison)
        if _table_exists(bind, schema, "establishments"):
            _add_column_if_missing(bind, schema, "establishments", sa.Column("latitude", sa.Float(), nullable=True))
            _add_column_if_missing(bind, schema, "establishments", sa.Column("longitude", sa.Float(), nullable=True))

        # --- delivery_zones : etablissement, mode de saisie
        if _table_exists(bind, schema, "delivery_zones"):
            _add_column_if_missing(
                bind, schema, "delivery_zones", sa.Column("establishment_id", sa.Integer(), nullable=True)
            )
            _add_column_if_missing(
                bind,
                schema,
                "delivery_zones",
                sa.Column("shape_kind", sa.String(16), nullable=False, server_default="polygon"),
            )
            _add_column_if_missing(bind, schema, "delivery_zones", sa.Column("shape_params", sa.JSON(), nullable=True))

            if _table_exists(bind, schema, "establishments"):
                if not _constraint_exists(bind, schema, "delivery_zones", "delivery_zones_establishment_id_fkey"):
                    op.create_foreign_key(
                        "delivery_zones_establishment_id_fkey",
                        "delivery_zones",
                        "establishments",
                        ["establishment_id"],
                        ["id"],
                        source_schema=schema,
                        referent_schema=schema,
                    )
                # Les zones existantes sont rattachees au premier etablissement actif : c'est deja
                # celui qui recevait toutes les commandes (resolution par defaut des commandes).
                bind.execute(
                    sa.text(
                        f"""UPDATE "{schema}".delivery_zones
                            SET establishment_id = (
                                SELECT id FROM "{schema}".establishments
                                WHERE is_active ORDER BY id LIMIT 1
                            )
                            WHERE establishment_id IS NULL"""
                    )
                )
            if not _index_exists(bind, schema, "ix_delivery_zones_establishment_id"):
                op.create_index(
                    "ix_delivery_zones_establishment_id", "delivery_zones", ["establishment_id"], schema=schema
                )
            if not _constraint_exists(bind, schema, "delivery_zones", "ck_delivery_zones_shape_kind"):
                op.create_check_constraint(
                    "ck_delivery_zones_shape_kind",
                    "delivery_zones",
                    "shape_kind IN ('polygon', 'circle', 'isochrone')",
                    schema=schema,
                )

            # --- delivery_zone_rules
            if not _table_exists(bind, schema, "delivery_zone_rules"):
                op.create_table(
                    "delivery_zone_rules",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("zone_id", sa.Integer(), nullable=False),
                    sa.Column("label", sa.String(128), nullable=False),
                    sa.Column("kind", sa.String(8), nullable=False),
                    sa.Column("fee", sa.Numeric(10, 2), nullable=True),
                    sa.Column("min_subtotal", sa.Numeric(10, 2), nullable=True),
                    sa.Column("days_of_week", sa.JSON(), nullable=True),
                    sa.Column("start_time", sa.Time(), nullable=True),
                    sa.Column("end_time", sa.Time(), nullable=True),
                    sa.Column("starts_on", sa.Date(), nullable=True),
                    sa.Column("ends_on", sa.Date(), nullable=True),
                    sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
                    sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
                    sa.ForeignKeyConstraint(
                        ["zone_id"],
                        [f"{schema}.delivery_zones.id"],
                        name="delivery_zone_rules_zone_id_fkey",
                        ondelete="CASCADE",
                    ),
                    sa.CheckConstraint("kind IN ('fee', 'free')", name="ck_delivery_zone_rules_kind"),
                    sa.CheckConstraint(
                        "(kind = 'fee' AND fee IS NOT NULL) OR kind = 'free'", name="ck_delivery_zone_rules_fee"
                    ),
                    schema=schema,
                )
                op.create_index("ix_delivery_zone_rules_zone_id", "delivery_zone_rules", ["zone_id"], schema=schema)

        # --- promotions : code « livraison offerte »
        if _table_exists(bind, schema, "promotions"):
            _add_column_if_missing(
                bind,
                schema,
                "promotions",
                sa.Column("free_delivery", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        if _column_exists(bind, schema, "promotions", "free_delivery"):
            op.drop_column("promotions", "free_delivery", schema=schema)

        if _table_exists(bind, schema, "delivery_zone_rules"):
            op.drop_table("delivery_zone_rules", schema=schema)

        if _table_exists(bind, schema, "delivery_zones"):
            if _constraint_exists(bind, schema, "delivery_zones", "ck_delivery_zones_shape_kind"):
                op.drop_constraint("ck_delivery_zones_shape_kind", "delivery_zones", schema=schema, type_="check")
            if _index_exists(bind, schema, "ix_delivery_zones_establishment_id"):
                op.drop_index("ix_delivery_zones_establishment_id", table_name="delivery_zones", schema=schema)
            if _constraint_exists(bind, schema, "delivery_zones", "delivery_zones_establishment_id_fkey"):
                op.drop_constraint(
                    "delivery_zones_establishment_id_fkey", "delivery_zones", schema=schema, type_="foreignkey"
                )
            for column in ("shape_params", "shape_kind", "establishment_id"):
                if _column_exists(bind, schema, "delivery_zones", column):
                    op.drop_column("delivery_zones", column, schema=schema)

        for column in ("longitude", "latitude"):
            if _column_exists(bind, schema, "establishments", column):
                op.drop_column("establishments", column, schema=schema)
