"""Add isolated, inactive HACCP/stock foundations per establishment.

Revision ID: haccp_p1_20261003
Revises: 0078
No strict approvals, lot quantities or staff permissions are inferred.
DDL is frozen here; never import mutable application models into a migration.
"""

from alembic import op
import sqlalchemy as sa

revision = "haccp_p1_20261003"
down_revision = "0078"
branch_labels = None
depends_on = None


def _seed_unambiguous_snapshot(bind):
    """Assignment is not reconciliation; no lot remainder or approval is invented."""
    bind.execute(
        sa.text("""INSERT INTO establishment_operations (establishment_id)
        SELECT id FROM establishments ON CONFLICT DO NOTHING""")
    )
    sites = list(bind.execute(sa.text("SELECT id FROM establishments")))
    if len(sites) != 1:
        return
    site = sites[0][0]
    bind.execute(
        sa.text("""INSERT INTO establishment_stock
        (establishment_id, ingredient_id, physical_quantity)
        SELECT :site, id, current_qty FROM ingredients WHERE current_qty IS NOT NULL
        ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    bind.execute(
        sa.text("""INSERT INTO stock_batch_positions
        (batch_id, establishment_id, ingredient_id)
        SELECT b.id, :site, b.ingredient_id FROM ingredient_batches b
        JOIN establishment_stock s ON s.ingredient_id=b.ingredient_id AND s.establishment_id=:site
        ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    bind.execute(
        sa.text("""INSERT INTO haccp_equipment_scopes (equipment_id, establishment_id)
        SELECT id, :site FROM haccp_equipment ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    bind.execute(
        sa.text("""INSERT INTO haccp_cleaning_scopes (task_id, establishment_id)
        SELECT id, :site FROM haccp_cleaning_tasks ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    # Duplicate/invalid legacy slots are left for the diagnostic; never choose one arbitrarily.
    bind.execute(
        sa.text("""INSERT INTO establishment_business_hours
        (establishment_id, day_of_week, slot_index, opens_at, closes_at, is_active)
        SELECT :site, h.day_of_week, h.slot_index, h.opens_at, h.closes_at, h.is_active
        FROM business_hours h
        WHERE h.day_of_week BETWEEN 0 AND 6 AND h.slot_index >= 0 AND h.closes_at > h.opens_at
        AND (SELECT count(*) FROM business_hours h2 WHERE h2.day_of_week=h.day_of_week
             AND h2.slot_index=h.slot_index)=1
        ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    bind.execute(
        sa.text("""INSERT INTO establishment_closures
        (establishment_id, closure_date, message)
        SELECT :site, closure_date, CASE WHEN use_default_message THEN
          (SELECT default_closure_message FROM tenant_config ORDER BY id LIMIT 1)
          ELSE custom_message END FROM exceptional_closures
        ON CONFLICT DO NOTHING"""),
        {"site": site},
    )
    bind.execute(
        sa.text("""UPDATE establishment_operations SET
        is_temporarily_closed=COALESCE((SELECT is_temporarily_closed FROM tenant_config ORDER BY id LIMIT 1), false),
        closure_message=(SELECT COALESCE(temporary_closure_message, default_closure_message)
                         FROM tenant_config ORDER BY id LIMIT 1)
        WHERE establishment_id=:site AND strict_enabled=false AND revision=1"""),
        {"site": site},
    )


def _upgrade_schema(bind, schema):
    previous = bind.scalar(sa.text("SELECT current_setting('search_path')"))
    quoted = bind.dialect.identifier_preparer.quote_schema(schema)
    bind.execute(
        sa.text("SELECT set_config('search_path', :path, true)"), {"path": quoted}
    )
    try:
        for table, columns, name in (
            ("orders", ["id", "establishment_id"], "uq_orders_id_establishment"),
            ("ingredient_batches", ["id", "ingredient_id"], "uq_batch_id_ingredient"),
        ):
            names = {
                constraint["name"]
                for constraint in sa.inspect(bind).get_unique_constraints(table)
            }
            if name not in names:
                op.create_unique_constraint(name, table, columns)
        _create_tables(bind)
        _seed_unambiguous_snapshot(bind)
    finally:
        # On DDL failure PostgreSQL aborts the transaction: caller rolls it back.
        # Do not mask that original failure with another query in an aborted transaction.
        if not bind.invalidated:
            import sys

            if sys.exc_info()[0] is None:
                bind.execute(
                    sa.text("SELECT set_config('search_path', :path, true)"),
                    {"path": previous},
                )


def upgrade():
    bind = op.get_bind()
    slugs = list(bind.execute(sa.text("SELECT slug FROM public.tenants ORDER BY id")))
    for (slug,) in slugs:
        _upgrade_schema(bind, f"tenant_{slug}")


def downgrade():
    raise RuntimeError(
        "HACCP foundation migration is forward-only to preserve audit/stock evidence. "
        "Roll back the application with strict mode disabled, leaving additive tables in place; "
        "use a verified isolated backup restore only if schema removal is required."
    )


def _create_tables(bind):
    if not sa.inspect(bind).has_table("establishment_business_hours"):
        op.create_table(
            "establishment_business_hours",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("day_of_week", sa.Integer(), nullable=False),
            sa.Column("slot_index", sa.Integer(), nullable=False),
            sa.Column("opens_at", sa.Time(), nullable=False),
            sa.Column("closes_at", sa.Time(), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.CheckConstraint("closes_at > opens_at", name="ck_site_hours_order"),
            sa.CheckConstraint(
                "day_of_week BETWEEN 0 AND 6 AND slot_index >= 0",
                name="ck_site_hours_day_slot",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "establishment_id",
                "day_of_week",
                "slot_index",
                name="uq_site_hours_slot",
            ),
        )
    if not sa.inspect(bind).has_table("establishment_closures"):
        op.create_table(
            "establishment_closures",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("closure_date", sa.Date(), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "establishment_id", "closure_date", name="uq_site_closure_day"
            ),
        )
    if not sa.inspect(bind).has_table("establishment_operations"):
        op.create_table(
            "establishment_operations",
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column(
                "strict_enabled", sa.Boolean(), server_default="false", nullable=False
            ),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "is_temporarily_closed",
                sa.Boolean(),
                server_default="false",
                nullable=False,
            ),
            sa.Column("closure_message", sa.Text(), nullable=True),
            sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
            sa.CheckConstraint(
                "NOT strict_enabled OR activated_at IS NOT NULL",
                name="ck_operations_activation",
            ),
            sa.CheckConstraint("revision > 0", name="ck_operations_revision"),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("establishment_id"),
        )
    if not sa.inspect(bind).has_table("establishment_stock"):
        op.create_table(
            "establishment_stock",
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), nullable=False),
            sa.Column(
                "physical_quantity", sa.Numeric(precision=12, scale=3), nullable=False
            ),
            sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
            sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reconciled_by", sa.Integer(), nullable=True),
            sa.Column(
                "captured_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "(reconciled_at IS NULL AND reconciled_by IS NULL) OR (reconciled_at IS NOT NULL AND reconciled_by IS NOT NULL)",
                name="ck_site_stock_reconciliation",
            ),
            sa.CheckConstraint("physical_quantity >= 0", name="ck_site_stock_quantity"),
            sa.CheckConstraint("revision > 0", name="ck_site_stock_revision"),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.ForeignKeyConstraint(
                ["ingredient_id"],
                ["ingredients.id"],
            ),
            sa.PrimaryKeyConstraint("establishment_id", "ingredient_id"),
        )
    if not sa.inspect(bind).has_table("haccp_cleaning_scopes"):
        op.create_table(
            "haccp_cleaning_scopes",
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("due_from", sa.Date(), nullable=True),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.ForeignKeyConstraint(
                ["task_id"],
                ["haccp_cleaning_tasks.id"],
            ),
            sa.PrimaryKeyConstraint("task_id"),
            sa.UniqueConstraint(
                "task_id", "establishment_id", name="uq_cleaning_scope"
            ),
        )
    if not sa.inspect(bind).has_table("haccp_daily_sessions"):
        op.create_table(
            "haccp_daily_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("business_date", sa.Date(), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("session_type", sa.String(length=16), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                server_default="in_progress",
                nullable=False,
            ),
            sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
            sa.Column("validated_by", sa.Integer(), nullable=True),
            sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "(status = 'in_progress' AND validated_by IS NULL AND validated_at IS NULL) OR (status IN ('complete','regularized') AND validated_by IS NOT NULL AND validated_at IS NOT NULL)",
                name="ck_haccp_daily_validation",
            ),
            sa.CheckConstraint(
                "session_type IN ('opening','closing')", name="ck_haccp_daily_type"
            ),
            sa.CheckConstraint(
                "status <> 'regularized' OR session_type = 'closing'",
                name="ck_haccp_daily_regularized",
            ),
            sa.CheckConstraint(
                "status IN ('in_progress','complete','regularized')",
                name="ck_haccp_daily_status",
            ),
            sa.CheckConstraint("revision > 0", name="ck_haccp_daily_revision"),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "establishment_id",
                "business_date",
                "session_type",
                name="uq_haccp_daily_site_date_type",
            ),
            sa.UniqueConstraint(
                "id",
                "establishment_id",
                "session_type",
                name="uq_haccp_daily_scope_type",
            ),
            sa.UniqueConstraint("id", "establishment_id", name="uq_haccp_daily_scope"),
        )
    if not sa.inspect(bind).has_table("haccp_equipment_scopes"):
        op.create_table(
            "haccp_equipment_scopes",
            sa.Column("equipment_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["equipment_id"],
                ["haccp_equipment.id"],
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("equipment_id"),
            sa.UniqueConstraint(
                "equipment_id", "establishment_id", name="uq_equipment_scope"
            ),
        )
    if not sa.inspect(bind).has_table("haccp_reminder_deliveries"):
        op.create_table(
            "haccp_reminder_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("business_date", sa.Date(), nullable=False),
            sa.Column("schedule_revision", sa.Integer(), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=False),
            sa.Column(
                "status", sa.String(length=16), server_default="pending", nullable=False
            ),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "channel IN ('push','in_app')", name="ck_haccp_reminder_channel"
            ),
            sa.CheckConstraint(
                "status <> 'sent' OR sent_at IS NOT NULL", name="ck_haccp_reminder_sent"
            ),
            sa.CheckConstraint(
                "status IN ('pending','sending','sent','failed','obsolete')",
                name="ck_haccp_reminder_status",
            ),
            sa.CheckConstraint(
                "attempts >= 0 AND schedule_revision > 0",
                name="ck_haccp_reminder_counters",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "establishment_id",
                "business_date",
                "schedule_revision",
                "due_at",
                "user_id",
                "channel",
                name="uq_haccp_reminder_delivery",
            ),
        )
        op.create_index(
            "ix_haccp_reminder_pending",
            "haccp_reminder_deliveries",
            ["status", "due_at"],
            unique=False,
        )
    if not sa.inspect(bind).has_table("haccp_staff_scopes"):
        op.create_table(
            "haccp_staff_scopes",
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("assigned_by", sa.Integer(), nullable=False),
            sa.Column(
                "assigned_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
            ),
            sa.PrimaryKeyConstraint("establishment_id", "user_id"),
        )
    if not sa.inspect(bind).has_table("haccp_audit_events"):
        op.create_table(
            "haccp_audit_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("subject", sa.String(length=128), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("before", sa.JSON(), nullable=True),
            sa.Column("after", sa.JSON(), nullable=True),
            sa.Column(
                "recorded_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "length(trim(action)) > 0 AND length(trim(reason)) > 0",
                name="ck_haccp_audit_explanation",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id"],
                ["establishments.id"],
            ),
            sa.ForeignKeyConstraint(
                ["session_id", "establishment_id"],
                ["haccp_daily_sessions.id", "haccp_daily_sessions.establishment_id"],
                name="fk_haccp_audit_session",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_haccp_audit_site_time",
            "haccp_audit_events",
            ["establishment_id", "recorded_at"],
            unique=False,
        )
    if not sa.inspect(bind).has_table("haccp_regularizations"):
        op.create_table(
            "haccp_regularizations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("closing_session_id", sa.Integer(), nullable=False),
            sa.Column(
                "session_type",
                sa.String(length=16),
                server_default="closing",
                nullable=False,
            ),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("actions_taken", sa.Text(), nullable=False),
            sa.Column("recorded_by", sa.Integer(), nullable=False),
            sa.Column(
                "recorded_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "session_type = 'closing'", name="ck_regularization_closing"
            ),
            sa.CheckConstraint(
                "length(trim(reason)) > 0 AND length(trim(actions_taken)) > 0",
                name="ck_regularization_explanation",
            ),
            sa.ForeignKeyConstraint(
                ["closing_session_id", "establishment_id", "session_type"],
                [
                    "haccp_daily_sessions.id",
                    "haccp_daily_sessions.establishment_id",
                    "haccp_daily_sessions.session_type",
                ],
                name="fk_regularization_closing",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("closing_session_id", name="uq_regularization_closing"),
        )
    if not sa.inspect(bind).has_table("haccp_requirements"):
        op.create_table(
            "haccp_requirements",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("session_revision", sa.Integer(), nullable=False),
            sa.Column("requirement_key", sa.String(length=128), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("equipment_id", sa.Integer(), nullable=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("ingredient_id", sa.Integer(), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.CheckConstraint(
                "(kind = 'temperature' AND equipment_id IS NOT NULL AND task_id IS NULL AND ingredient_id IS NULL) OR (kind = 'cleaning' AND task_id IS NOT NULL AND equipment_id IS NULL AND ingredient_id IS NULL) OR (kind = 'dlc' AND ingredient_id IS NOT NULL AND equipment_id IS NULL AND task_id IS NULL)",
                name="ck_requirement_target",
            ),
            sa.CheckConstraint("session_revision > 0", name="ck_requirement_revision"),
            sa.ForeignKeyConstraint(
                ["equipment_id", "establishment_id"],
                [
                    "haccp_equipment_scopes.equipment_id",
                    "haccp_equipment_scopes.establishment_id",
                ],
                name="fk_requirement_equipment",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id", "ingredient_id"],
                [
                    "establishment_stock.establishment_id",
                    "establishment_stock.ingredient_id",
                ],
                name="fk_requirement_stock",
            ),
            sa.ForeignKeyConstraint(
                ["session_id", "establishment_id"],
                ["haccp_daily_sessions.id", "haccp_daily_sessions.establishment_id"],
                name="fk_requirement_session",
            ),
            sa.ForeignKeyConstraint(
                ["task_id", "establishment_id"],
                [
                    "haccp_cleaning_scopes.task_id",
                    "haccp_cleaning_scopes.establishment_id",
                ],
                name="fk_requirement_task",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "id",
                "establishment_id",
                "ingredient_id",
                name="uq_requirement_ingredient",
            ),
            sa.UniqueConstraint(
                "id", "session_id", "establishment_id", name="uq_requirement_scope"
            ),
            sa.UniqueConstraint(
                "session_id",
                "session_revision",
                "requirement_key",
                name="uq_requirement_revision_key",
            ),
        )
    if not sa.inspect(bind).has_table("stock_batch_positions"):
        op.create_table(
            "stock_batch_positions",
            sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), nullable=False),
            sa.Column(
                "remaining_quantity", sa.Numeric(precision=12, scale=3), nullable=True
            ),
            sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
            sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("excluded_by", sa.Integer(), nullable=True),
            sa.Column("exclusion_reason", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "(excluded_at IS NULL AND excluded_by IS NULL AND exclusion_reason IS NULL) OR (excluded_at IS NOT NULL AND excluded_by IS NOT NULL AND exclusion_reason IS NOT NULL AND length(trim(exclusion_reason)) > 0)",
                name="ck_position_exclusion_proof",
            ),
            sa.CheckConstraint("remaining_quantity >= 0", name="ck_position_quantity"),
            sa.CheckConstraint("revision > 0", name="ck_position_revision"),
            sa.ForeignKeyConstraint(
                ["batch_id", "ingredient_id"],
                ["ingredient_batches.id", "ingredient_batches.ingredient_id"],
                name="fk_position_batch_ingredient",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id", "ingredient_id"],
                [
                    "establishment_stock.establishment_id",
                    "establishment_stock.ingredient_id",
                ],
                name="fk_position_site_stock",
            ),
            sa.PrimaryKeyConstraint("batch_id"),
            sa.UniqueConstraint(
                "batch_id",
                "establishment_id",
                "ingredient_id",
                name="uq_position_scope",
            ),
        )
        op.create_index(
            "ix_position_site_ingredient",
            "stock_batch_positions",
            ["establishment_id", "ingredient_id"],
            unique=False,
        )
    if not sa.inspect(bind).has_table("establishment_stock_movements"):
        op.create_table(
            "establishment_stock_movements",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), nullable=True),
            sa.Column(
                "quantity_delta", sa.Numeric(precision=12, scale=3), nullable=False
            ),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "length(trim(idempotency_key)) > 0", name="ck_site_movement_key"
            ),
            sa.CheckConstraint(
                "length(trim(reason)) > 0", name="ck_site_movement_reason"
            ),
            sa.CheckConstraint("quantity_delta <> 0", name="ck_site_movement_delta"),
            sa.ForeignKeyConstraint(
                ["batch_id", "establishment_id", "ingredient_id"],
                [
                    "stock_batch_positions.batch_id",
                    "stock_batch_positions.establishment_id",
                    "stock_batch_positions.ingredient_id",
                ],
                name="fk_site_movement_position",
            ),
            sa.ForeignKeyConstraint(
                ["establishment_id", "ingredient_id"],
                [
                    "establishment_stock.establishment_id",
                    "establishment_stock.ingredient_id",
                ],
                name="fk_site_movement_stock",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "establishment_id", "idempotency_key", name="uq_site_movement_request"
            ),
        )
        op.create_index(
            "ix_site_movement_history",
            "establishment_stock_movements",
            ["establishment_id", "ingredient_id", "created_at"],
            unique=False,
        )
    if not sa.inspect(bind).has_table("haccp_evidence"):
        op.create_table(
            "haccp_evidence",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("is_compliant", sa.Boolean(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("recorded_by", sa.Integer(), nullable=False),
            sa.Column(
                "recorded_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "length(trim(idempotency_key)) > 0", name="ck_evidence_key"
            ),
            sa.ForeignKeyConstraint(
                ["requirement_id", "session_id", "establishment_id"],
                [
                    "haccp_requirements.id",
                    "haccp_requirements.session_id",
                    "haccp_requirements.establishment_id",
                ],
                name="fk_evidence_requirement",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "session_id", "idempotency_key", name="uq_evidence_request"
            ),
        )
        op.create_index(
            "ix_evidence_requirement",
            "haccp_evidence",
            ["requirement_id", "recorded_at"],
            unique=False,
        )
    if not sa.inspect(bind).has_table("haccp_requirement_batches"):
        op.create_table(
            "haccp_requirement_batches",
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), nullable=False),
            sa.Column("batch_revision", sa.Integer(), nullable=False),
            sa.Column(
                "effective_expires_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.CheckConstraint(
                "batch_revision > 0", name="ck_requirement_batch_revision"
            ),
            sa.ForeignKeyConstraint(
                ["batch_id", "establishment_id", "ingredient_id"],
                [
                    "stock_batch_positions.batch_id",
                    "stock_batch_positions.establishment_id",
                    "stock_batch_positions.ingredient_id",
                ],
                name="fk_requirement_batch_position",
            ),
            sa.ForeignKeyConstraint(
                ["requirement_id", "establishment_id", "ingredient_id"],
                [
                    "haccp_requirements.id",
                    "haccp_requirements.establishment_id",
                    "haccp_requirements.ingredient_id",
                ],
                name="fk_requirement_batch_requirement",
            ),
            sa.PrimaryKeyConstraint("requirement_id", "batch_id"),
        )
    if not sa.inspect(bind).has_table("stock_batch_allocations"):
        op.create_table(
            "stock_batch_allocations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("establishment_id", sa.Integer(), nullable=False),
            sa.Column("ingredient_id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column(
                "restored_quantity",
                sa.Numeric(precision=12, scale=3),
                server_default="0",
                nullable=False,
            ),
            sa.Column("allocated_by", sa.Integer(), nullable=True),
            sa.Column(
                "allocated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint("quantity > 0", name="ck_allocation_quantity"),
            sa.CheckConstraint(
                "restored_quantity >= 0 AND restored_quantity <= quantity",
                name="ck_allocation_restored",
            ),
            sa.ForeignKeyConstraint(
                ["batch_id", "establishment_id", "ingredient_id"],
                [
                    "stock_batch_positions.batch_id",
                    "stock_batch_positions.establishment_id",
                    "stock_batch_positions.ingredient_id",
                ],
                name="fk_allocation_position",
            ),
            sa.ForeignKeyConstraint(
                ["order_id", "establishment_id"],
                ["orders.id", "orders.establishment_id"],
                name="fk_allocation_order_site",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "order_id", "batch_id", name="uq_allocation_order_batch"
            ),
        )
        op.create_index(
            "ix_allocation_site_order",
            "stock_batch_allocations",
            ["establishment_id", "order_id"],
            unique=False,
        )
