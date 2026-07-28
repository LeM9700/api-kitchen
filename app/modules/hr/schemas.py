"""Schemas Pydantic du module RH."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmployeeProfileCreate(BaseModel):
    user_id: int
    establishment_id: int
    hourly_rate_cents: int | None = None
    weekly_hours_contract: int = 35
    hire_date: date | None = None


class EmployeeProfileUpdate(BaseModel):
    hourly_rate_cents: int | None = None
    weekly_hours_contract: int | None = None
    establishment_id: int | None = None
    is_active: bool | None = None


class EmployeeProfileOut(BaseModel):
    """Vue admin: inclut le cout horaire."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    establishment_id: int
    hourly_rate_cents: int | None
    weekly_hours_contract: int
    hire_date: date | None
    is_active: bool
    created_at: datetime


class EmployeeProfileSelfOut(BaseModel):
    """Vue employee: jamais de cout horaire."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    establishment_id: int
    weekly_hours_contract: int
    hire_date: date | None
    is_active: bool


class ShiftCreate(BaseModel):
    employee_id: int
    establishment_id: int
    starts_at: datetime
    ends_at: datetime
    break_minutes: int = 0

    def model_post_init(self, __context) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")


class ShiftUpdate(BaseModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    break_minutes: int | None = None
    status: Literal["scheduled", "cancelled"] | None = None


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    establishment_id: int
    starts_at: datetime
    ends_at: datetime
    break_minutes: int
    status: str


class ClockInRequest(BaseModel):
    method: Literal["web", "mobile", "qrcode"]
    establishment_id: int
    shift_id: int | None = None


class TimeClockEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    shift_id: int | None
    establishment_id: int
    clock_in_at: datetime
    clock_out_at: datetime | None
    method: str
    status: str


class TimeClockCorrectionRequest(BaseModel):
    new_clock_in_at: datetime | None = None
    new_clock_out_at: datetime | None = None
    reason: str = Field(min_length=1)
