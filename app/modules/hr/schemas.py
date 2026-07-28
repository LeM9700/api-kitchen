"""Schemas Pydantic du module RH."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


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
