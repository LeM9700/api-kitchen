"""Daily, establishment-scoped foundations, not yet used by legacy HACCP routes.

Separate tables preserve the daily legacy singleton queries until phase 2.
Composite foreign keys prevent cross-establishment evidence and allocations.
No historical session is promoted into a strict approval by migration.
"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EstablishmentOperations(Base):
    __tablename__ = "establishment_operations"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_operations_revision"),
        CheckConstraint(
            "NOT strict_enabled OR activated_at IS NOT NULL",
            name="ck_operations_activation",
        ),
    )

    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), primary_key=True
    )
    strict_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_temporarily_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    closure_message: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class EstablishmentBusinessHours(Base):
    __tablename__ = "establishment_business_hours"
    __table_args__ = (
        UniqueConstraint(
            "establishment_id", "day_of_week", "slot_index", name="uq_site_hours_slot"
        ),
        CheckConstraint(
            "day_of_week BETWEEN 0 AND 6 AND slot_index >= 0",
            name="ck_site_hours_day_slot",
        ),
        CheckConstraint("closes_at > opens_at", name="ck_site_hours_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    opens_at: Mapped[time] = mapped_column(Time, nullable=False)
    closes_at: Mapped[time] = mapped_column(Time, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )


class EstablishmentClosure(Base):
    __tablename__ = "establishment_closures"
    __table_args__ = (
        UniqueConstraint(
            "establishment_id", "closure_date", name="uq_site_closure_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    closure_date: Mapped[date] = mapped_column(Date, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)


class HaccpEquipmentScope(Base):
    __tablename__ = "haccp_equipment_scopes"
    __table_args__ = (
        UniqueConstraint("equipment_id", "establishment_id", name="uq_equipment_scope"),
    )

    equipment_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_equipment.id"), primary_key=True
    )
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )


class HaccpCleaningScope(Base):
    __tablename__ = "haccp_cleaning_scopes"
    __table_args__ = (
        UniqueConstraint("task_id", "establishment_id", name="uq_cleaning_scope"),
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_cleaning_tasks.id"), primary_key=True
    )
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    # Weekly/monthly schedules need an explicit anchor; NULL is unresolved.
    due_from: Mapped[date | None] = mapped_column(Date)


class HaccpStaffScope(Base):
    __tablename__ = "haccp_staff_scopes"

    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    # Membership is not a permission: require_permission still applies in phase 2.
    assigned_by: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HaccpDailySession(Base):
    __tablename__ = "haccp_daily_sessions"
    __table_args__ = (
        UniqueConstraint(
            "establishment_id",
            "business_date",
            "session_type",
            name="uq_haccp_daily_site_date_type",
        ),
        UniqueConstraint("id", "establishment_id", name="uq_haccp_daily_scope"),
        UniqueConstraint(
            "id", "establishment_id", "session_type", name="uq_haccp_daily_scope_type"
        ),
        CheckConstraint(
            "session_type IN ('opening','closing')", name="ck_haccp_daily_type"
        ),
        CheckConstraint(
            "status IN ('in_progress','complete','regularized')",
            name="ck_haccp_daily_status",
        ),
        CheckConstraint(
            "status <> 'regularized' OR session_type = 'closing'",
            name="ck_haccp_daily_regularized",
        ),
        CheckConstraint("revision > 0", name="ck_haccp_daily_revision"),
        CheckConstraint(
            "(status = 'in_progress' AND validated_by IS NULL AND validated_at IS NULL) OR "
            "(status IN ('complete','regularized') AND validated_by IS NOT NULL AND validated_at IS NOT NULL)",
            name="ck_haccp_daily_validation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="in_progress"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    validated_by: Mapped[int | None] = mapped_column(Integer)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HaccpRequirement(Base):
    __tablename__ = "haccp_requirements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "establishment_id"],
            ["haccp_daily_sessions.id", "haccp_daily_sessions.establishment_id"],
            name="fk_requirement_session",
        ),
        ForeignKeyConstraint(
            ["equipment_id", "establishment_id"],
            [
                "haccp_equipment_scopes.equipment_id",
                "haccp_equipment_scopes.establishment_id",
            ],
            name="fk_requirement_equipment",
        ),
        ForeignKeyConstraint(
            ["task_id", "establishment_id"],
            ["haccp_cleaning_scopes.task_id", "haccp_cleaning_scopes.establishment_id"],
            name="fk_requirement_task",
        ),
        ForeignKeyConstraint(
            ["establishment_id", "ingredient_id"],
            [
                "establishment_stock.establishment_id",
                "establishment_stock.ingredient_id",
            ],
            name="fk_requirement_stock",
        ),
        UniqueConstraint(
            "session_id",
            "session_revision",
            "requirement_key",
            name="uq_requirement_revision_key",
        ),
        UniqueConstraint(
            "id", "session_id", "establishment_id", name="uq_requirement_scope"
        ),
        UniqueConstraint(
            "id", "establishment_id", "ingredient_id", name="uq_requirement_ingredient"
        ),
        CheckConstraint("session_revision > 0", name="ck_requirement_revision"),
        CheckConstraint(
            "(kind = 'temperature' AND equipment_id IS NOT NULL AND task_id IS NULL AND ingredient_id IS NULL) OR "
            "(kind = 'cleaning' AND task_id IS NOT NULL AND equipment_id IS NULL AND ingredient_id IS NULL) OR "
            "(kind = 'dlc' AND ingredient_id IS NOT NULL AND equipment_id IS NULL AND task_id IS NULL)",
            name="ck_requirement_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    equipment_id: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[int | None] = mapped_column(Integer)
    ingredient_id: Mapped[int | None] = mapped_column(Integer)
    # Frozen expected limits, lot IDs/revisions and task due dates, never a live ORM view.
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)


class HaccpRequirementBatch(Base):
    """Relational lot snapshot: JSON alone cannot enforce lot/site ownership."""

    __tablename__ = "haccp_requirement_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["requirement_id", "establishment_id", "ingredient_id"],
            [
                "haccp_requirements.id",
                "haccp_requirements.establishment_id",
                "haccp_requirements.ingredient_id",
            ],
            name="fk_requirement_batch_requirement",
        ),
        ForeignKeyConstraint(
            ["batch_id", "establishment_id", "ingredient_id"],
            [
                "stock_batch_positions.batch_id",
                "stock_batch_positions.establishment_id",
                "stock_batch_positions.ingredient_id",
            ],
            name="fk_requirement_batch_position",
        ),
        CheckConstraint("batch_revision > 0", name="ck_requirement_batch_revision"),
    )

    requirement_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ingredient_id: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class HaccpEvidence(Base):
    __tablename__ = "haccp_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["requirement_id", "session_id", "establishment_id"],
            [
                "haccp_requirements.id",
                "haccp_requirements.session_id",
                "haccp_requirements.establishment_id",
            ],
            name="fk_evidence_requirement",
        ),
        UniqueConstraint("session_id", "idempotency_key", name="uq_evidence_request"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_evidence_key"),
        Index("ix_evidence_requirement", "requirement_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement_id: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    is_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    recorded_by: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HaccpRegularization(Base):
    __tablename__ = "haccp_regularizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["closing_session_id", "establishment_id", "session_type"],
            [
                "haccp_daily_sessions.id",
                "haccp_daily_sessions.establishment_id",
                "haccp_daily_sessions.session_type",
            ],
            name="fk_regularization_closing",
        ),
        UniqueConstraint("closing_session_id", name="uq_regularization_closing"),
        CheckConstraint("session_type = 'closing'", name="ck_regularization_closing"),
        CheckConstraint(
            "length(trim(reason)) > 0 AND length(trim(actions_taken)) > 0",
            name="ck_regularization_explanation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    closing_session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="closing"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actions_taken: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HaccpAuditEvent(Base):
    __tablename__ = "haccp_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "establishment_id"],
            ["haccp_daily_sessions.id", "haccp_daily_sessions.establishment_id"],
            name="fk_haccp_audit_session",
        ),
        CheckConstraint(
            "length(trim(action)) > 0 AND length(trim(reason)) > 0",
            name="ck_haccp_audit_explanation",
        ),
        Index("ix_haccp_audit_site_time", "establishment_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    session_id: Mapped[int | None] = mapped_column(Integer)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HaccpReminderDelivery(Base):
    __tablename__ = "haccp_reminder_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "establishment_id",
            "business_date",
            "schedule_revision",
            "due_at",
            "user_id",
            "channel",
            name="uq_haccp_reminder_delivery",
        ),
        CheckConstraint(
            "channel IN ('push','in_app')", name="ck_haccp_reminder_channel"
        ),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','obsolete')",
            name="ck_haccp_reminder_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND schedule_revision > 0", name="ck_haccp_reminder_counters"
        ),
        CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL", name="ck_haccp_reminder_sent"
        ),
        Index("ix_haccp_reminder_pending", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
