"""Modèles SQLAlchemy du module RH : employés, planning, pointage, alertes.

Aucune FK vers `users` — user_id/created_by_user_id/corrected_by_user_id
sont stockés en Integer nu et résolus côté service, comme dans les
modules stock/notifications (évite le couplage cross-module).
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Establishment(Base):
    __tablename__ = "establishments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Paris")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmployeeProfile(Base):
    __tablename__ = "employee_profiles"
    __table_args__ = (
        Index("ix_employee_profiles_user_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    establishment_id: Mapped[int] = mapped_column(ForeignKey("establishments.id"), nullable=False)
    hourly_rate_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_hours_contract: Mapped[int] = mapped_column(Integer, nullable=False, default=35)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_employee_starts_at", "employee_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee_profiles.id"), nullable=False)
    establishment_id: Mapped[int] = mapped_column(ForeignKey("establishments.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TimeClockEntry(Base):
    __tablename__ = "time_clock_entries"
    __table_args__ = (
        Index("ix_time_clock_entries_employee_clock_in", "employee_id", "clock_in_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee_profiles.id"), nullable=False)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True)
    establishment_id: Mapped[int] = mapped_column(ForeignKey("establishments.id"), nullable=False)
    clock_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TimeClockCorrection(Base):
    __tablename__ = "time_clock_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("time_clock_entries.id"), nullable=False)
    corrected_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    old_clock_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    old_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_clock_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HrAlert(Base):
    __tablename__ = "hr_alerts"
    __table_args__ = (
        Index("ix_hr_alerts_employee_type", "employee_id", "type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee_profiles.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EstablishmentHrConfig(Base):
    __tablename__ = "establishment_hr_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), nullable=False, unique=True
    )
    weekly_hours_legal_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=35)
    late_tolerance_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    alert_cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    labor_cost_target_ratio: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0.30)
