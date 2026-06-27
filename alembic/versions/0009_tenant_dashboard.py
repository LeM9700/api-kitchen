"""add tenant_config, business_hours, exceptional_closures to tenant schemas

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0009a"
down_revision = "0008"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    """Récupère tous les slugs de tenants existants depuis le schema public.

    Args:
        bind: Connexion SQLAlchemy active (``op.get_bind()``).

    Returns:
        Liste des slugs de tenants.
    """
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Crée les tables ``tenant_config``, ``business_hours`` et ``exceptional_closures``
    dans chaque schema tenant existant.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure ces tables
    dans leur script de provisioning (``create_tenant_schema``).
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # --- tenant_config (ligne unique par tenant, upsert pattern) ---
        op.create_table(
            "tenant_config",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("is_temporarily_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("temporary_closure_message", sa.Text(), nullable=True),
            sa.Column(
                "default_closure_message",
                sa.Text(),
                nullable=False,
                server_default="Nous sommes temporairement fermés. Nous vous accueillons bientôt !",
            ),
            sa.Column("prep_time_normal_minutes", sa.Integer(), nullable=False, server_default="25"),
            sa.Column("prep_time_peak_minutes", sa.Integer(), nullable=False, server_default="45"),
            sa.Column("peak_orders_threshold", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("auto_calc_prep_time", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("overhead_per_order_minutes", sa.Integer(), nullable=False, server_default="3"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )

        # --- business_hours (créneaux multi-slots par jour) ---
        op.create_table(
            "business_hours",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("day_of_week", sa.Integer(), nullable=False),
            sa.Column("slot_index", sa.Integer(), nullable=False),
            sa.Column("opens_at", sa.Time(), nullable=False),
            sa.Column("closes_at", sa.Time(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.CheckConstraint("closes_at > opens_at", name=f"ck_business_hours_closes_after_opens_{slug}"),
            schema=schema,
        )
        op.create_index(
            f"ix_business_hours_day_slot_{slug}",
            "business_hours",
            ["day_of_week", "slot_index"],
            schema=schema,
        )

        # --- exceptional_closures (fermetures ponctuelles, date unique) ---
        op.create_table(
            "exceptional_closures",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("closure_date", sa.Date(), nullable=False, unique=True),
            sa.Column("custom_message", sa.Text(), nullable=True),
            sa.Column("use_default_message", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            schema=schema,
        )
        op.create_index(
            f"ix_exceptional_closures_date_{slug}",
            "exceptional_closures",
            ["closure_date"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime les tables ``exceptional_closures``, ``business_hours`` et ``tenant_config``
    de chaque schema tenant existant (ordre inverse pour respecter les contraintes).
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        op.drop_index(f"ix_exceptional_closures_date_{slug}", table_name="exceptional_closures", schema=schema)
        op.drop_table("exceptional_closures", schema=schema)

        op.drop_index(f"ix_business_hours_day_slot_{slug}", table_name="business_hours", schema=schema)
        op.drop_table("business_hours", schema=schema)

        op.drop_table("tenant_config", schema=schema)
