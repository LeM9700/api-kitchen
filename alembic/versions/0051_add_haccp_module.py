"""Add HACCP food safety module tables (tenant-scoped)

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-21

Tables créées dans chaque schéma tenant :
- haccp_equipment           : équipements à contrôler (frigos, CF, etc.)
- haccp_check_sessions      : session ouverture/fermeture (gate bloquant)
- haccp_temperature_logs    : relevés de température par équipement
- haccp_dlc_checks          : vérification DLC 1/2/3 par ingrédient/batch
- haccp_cleaning_tasks      : plan ND défini par admin
- haccp_cleaning_logs       : réalisation des tâches ND
- haccp_non_conformities    : écarts + actions correctives
- haccp_reception_controls  : contrôles à réception fournisseurs
- haccp_cooling_logs        : refroidissement rapide
- haccp_training_records    : formation hygiène staff (arrêté 12/02/2024)
- haccp_frying_oil_logs     : suivi huiles friteuse (optionnel, feature flag)
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        _create_haccp_tables(schema)


def _create_haccp_tables(schema: str) -> None:
    # ── 1. Équipements ───────────────────────────────────────────────────────
    op.create_table(
        "haccp_equipment",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),          # fridge|freezer|cold_room|hot_hold|ambient
        sa.Column("location", sa.String(128), nullable=True),      # "Cuisine froide", "Comptoir"
        sa.Column("target_min_temp", sa.Float, nullable=True),
        sa.Column("target_max_temp", sa.Float, nullable=True),
        sa.Column("check_at_opening", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("check_at_closing", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('fridge','freezer','cold_room','hot_hold','ambient')",
            name=f"ck_haccp_equipment_type_{schema}",
        ),
        schema=schema,
    )
    op.create_index("ix_haccp_equipment_active", "haccp_equipment", ["is_active"], schema=schema)

    # ── 2. Sessions de contrôle (gate bloquant ouverture/fermeture) ─────────
    op.create_table(
        "haccp_check_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_type", sa.String(16), nullable=False),  # opening | closing
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("started_by", sa.Integer, nullable=True),        # FK users.id
        sa.Column("completed_by", sa.Integer, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "session_type IN ('opening','closing')",
            name=f"ck_haccp_sessions_type_{schema}",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress','complete','incomplete_validated')",
            name=f"ck_haccp_sessions_status_{schema}",
        ),
        sa.UniqueConstraint("date", "session_type", name=f"uq_haccp_sessions_date_type_{schema}"),
        schema=schema,
    )
    op.create_index("ix_haccp_check_sessions_date", "haccp_check_sessions", ["date"], schema=schema)

    # ── 3. Relevés de température ────────────────────────────────────────────
    op.create_table(
        "haccp_temperature_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_check_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("equipment_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_equipment.id"), nullable=False),
        sa.Column("measured_temp", sa.Float, nullable=False),
        sa.Column("is_compliant", sa.Boolean, nullable=False),     # calculé côté API
        sa.Column("corrective_action", sa.Text, nullable=True),
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )
    op.create_index("ix_haccp_temp_logs_session", "haccp_temperature_logs", ["session_id"], schema=schema)

    # ── 4. Vérifications DLC ─────────────────────────────────────────────────
    op.create_table(
        "haccp_dlc_checks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_check_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", sa.Integer, nullable=True),     # FK souple (pas de FK dure cross-module)
        sa.Column("batch_id", sa.Integer, nullable=True),
        sa.Column("ingredient_name", sa.String(128), nullable=False),  # dénormalisé pour lisibilité rapports
        sa.Column("dlc_level", sa.Integer, nullable=False),        # 1, 2, ou 3
        sa.Column("dlc_date", sa.Date, nullable=False),
        sa.Column("location", sa.String(128), nullable=True),      # "Frigo 2", "Table garniture"
        sa.Column("is_compliant", sa.Boolean, nullable=False),
        sa.Column("corrective_action", sa.Text, nullable=True),
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("dlc_level IN (1,2,3)", name=f"ck_haccp_dlc_level_{schema}"),
        schema=schema,
    )
    op.create_index("ix_haccp_dlc_checks_session", "haccp_dlc_checks", ["session_id"], schema=schema)

    # ── 5. Tâches de nettoyage (plan ND) ────────────────────────────────────
    op.create_table(
        "haccp_cleaning_tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("zone", sa.String(64), nullable=False),          # cuisine|comptoir|frigo|sol|sanitaires
        sa.Column("frequency", sa.String(32), nullable=False),     # daily|weekly|monthly|per_service
        sa.Column("session_type", sa.String(16), nullable=False, server_default="both"),  # opening|closing|both
        sa.Column("product_used", sa.String(128), nullable=True),
        sa.Column("required_role", sa.String(32), nullable=False, server_default="staff"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "frequency IN ('daily','weekly','monthly','per_service')",
            name=f"ck_haccp_cleaning_frequency_{schema}",
        ),
        sa.CheckConstraint(
            "session_type IN ('opening','closing','both')",
            name=f"ck_haccp_cleaning_session_type_{schema}",
        ),
        schema=schema,
    )

    # ── 6. Logs de réalisation des tâches ND ────────────────────────────────
    op.create_table(
        "haccp_cleaning_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_check_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_cleaning_tasks.id"), nullable=False),
        sa.Column("completed_by", sa.Integer, nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_compliant", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint("session_id", "task_id", name=f"uq_haccp_cleaning_log_{schema}"),
        schema=schema,
    )
    op.create_index("ix_haccp_cleaning_logs_session", "haccp_cleaning_logs", ["session_id"], schema=schema)

    # ── 7. Non-conformités + actions correctives ─────────────────────────────
    op.create_table(
        "haccp_non_conformities",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_check_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),   # temperature|dlc|cleaning|reception|cooling
        sa.Column("source_id", sa.Integer, nullable=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("corrective_action", sa.Text, nullable=True),
        sa.Column("validated_by", sa.Integer, nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),  # open|in_progress|closed
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "source_type IN ('temperature','dlc','cleaning','reception','cooling','other')",
            name=f"ck_haccp_nc_source_{schema}",
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','closed')",
            name=f"ck_haccp_nc_status_{schema}",
        ),
        schema=schema,
    )
    op.create_index("ix_haccp_nc_status", "haccp_non_conformities", ["status"], schema=schema)

    # ── 8. Contrôles à réception ─────────────────────────────────────────────
    op.create_table(
        "haccp_reception_controls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("supplier_name", sa.String(128), nullable=False),
        sa.Column("delivery_date", sa.Date, nullable=False),
        sa.Column("temperature_on_arrival", sa.Float, nullable=True),
        sa.Column("packaging_ok", sa.Boolean, nullable=False),
        sa.Column("labeling_ok", sa.Boolean, nullable=False),
        sa.Column("dlc_ok", sa.Boolean, nullable=False),
        sa.Column("is_accepted", sa.Boolean, nullable=False),
        sa.Column("refusal_reason", sa.Text, nullable=True),
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )
    op.create_index("ix_haccp_reception_date", "haccp_reception_controls", ["delivery_date"], schema=schema)

    # ── 9. Refroidissement rapide ─────────────────────────────────────────────
    op.create_table(
        "haccp_cooling_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_name", sa.String(128), nullable=False),
        sa.Column("quantity", sa.String(64), nullable=True),
        sa.Column("temp_start", sa.Float, nullable=False),         # °C au début (cuisson)
        sa.Column("temp_at_90min", sa.Float, nullable=True),       # objectif ≤10°C à t+2h
        sa.Column("temp_final", sa.Float, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_compliant", sa.Boolean, nullable=True),
        sa.Column("corrective_action", sa.Text, nullable=True),
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=schema,
    )

    # ── 10. Formation hygiène staff ──────────────────────────────────────────
    op.create_table(
        "haccp_training_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),          # FK souple vers users.id
        sa.Column("user_name", sa.String(128), nullable=True),     # dénormalisé
        sa.Column("training_type", sa.String(64), nullable=False), # hygiene_14h|refresher|haccp_module
        sa.Column("training_date", sa.Date, nullable=False),
        sa.Column("expiry_date", sa.Date, nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("certificate_ref", sa.String(128), nullable=True),
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "training_type IN ('hygiene_14h','refresher','haccp_module','other')",
            name=f"ck_haccp_training_type_{schema}",
        ),
        schema=schema,
    )
    op.create_index("ix_haccp_training_user", "haccp_training_records", ["user_id"], schema=schema)

    # ── 11. Suivi huiles de friture (optionnel) ───────────────────────────────
    op.create_table(
        "haccp_frying_oil_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey(f"{schema}.haccp_check_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fryer_name", sa.String(64), nullable=False),
        sa.Column("polarity_percent", sa.Float, nullable=True),    # test acidité (seuil légal <25%)
        sa.Column("color_ok", sa.Boolean, nullable=True),          # couleur visuelle
        sa.Column("odor_ok", sa.Boolean, nullable=True),
        sa.Column("is_compliant", sa.Boolean, nullable=False),
        sa.Column("action_taken", sa.String(32), nullable=True),   # none|filtered|replaced
        sa.Column("logged_by", sa.Integer, nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "action_taken IN ('none','filtered','replaced') OR action_taken IS NULL",
            name=f"ck_haccp_oil_action_{schema}",
        ),
        schema=schema,
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = [
        "haccp_frying_oil_logs",
        "haccp_training_records",
        "haccp_cooling_logs",
        "haccp_reception_controls",
        "haccp_non_conformities",
        "haccp_cleaning_logs",
        "haccp_cleaning_tasks",
        "haccp_dlc_checks",
        "haccp_temperature_logs",
        "haccp_check_sessions",
        "haccp_equipment",
    ]
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        for table in tables:
            op.execute(sa.text(f'DROP TABLE IF EXISTS "{schema}".{table} CASCADE'))
