# app/modules/admin/tenant_schemas.py
"""Schemas Pydantic pour le tableau de bord tenant self-service."""
from datetime import date, datetime, time, timezone

import pytz
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenantConfigUpdate(BaseModel):
    """Mise a jour partielle de la configuration tenant -- tous champs optionnels."""

    is_temporarily_closed: bool | None = None
    temporary_closure_message: str | None = None
    default_closure_message: str | None = None
    prep_time_normal_minutes: int | None = Field(None, ge=1, le=180)
    prep_time_peak_minutes: int | None = Field(None, ge=1, le=360)
    peak_orders_threshold: int | None = Field(None, ge=1, le=100)
    auto_calc_prep_time: bool | None = None
    overhead_per_order_minutes: int | None = Field(None, ge=0, le=60)
    timezone: str | None = None

    @field_validator("temporary_closure_message", "default_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v

    @field_validator("timezone", mode="before")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            pytz.timezone(v)
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"Timezone inconnue : {v!r}")
        return v


class TenantConfigResponse(BaseModel):
    """Representation complete de la configuration tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_temporarily_closed: bool
    temporary_closure_message: str | None
    default_closure_message: str
    prep_time_normal_minutes: int
    prep_time_peak_minutes: int
    peak_orders_threshold: int
    auto_calc_prep_time: bool
    overhead_per_order_minutes: int
    timezone: str
    updated_at: datetime
    scheduled_close_at: datetime | None


class TenantScheduledClosureRequest(BaseModel):
    scheduled_close_at: datetime | None
    temporary_closure_message: str | None = None

    @field_validator("scheduled_close_at")
    @classmethod
    def validate_scheduled_close_at(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("scheduled_close_at doit inclure une timezone")
        if v <= datetime.now(timezone.utc):
            raise ValueError("scheduled_close_at doit etre dans le futur")
        return v

    @field_validator("temporary_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class BusinessHoursCreate(BaseModel):
    slot_index: int = Field(..., ge=0, le=10)
    opens_at: time
    closes_at: time


class BusinessHoursResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_of_week: int
    slot_index: int
    opens_at: time
    closes_at: time
    is_active: bool


class ExceptionalClosureCreate(BaseModel):
    closure_date: date
    custom_message: str | None = None
    use_default_message: bool = False

    @field_validator("custom_message", mode="before")
    @classmethod
    def validate_custom_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class ExceptionalClosureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    closure_date: date
    custom_message: str | None
    use_default_message: bool
    created_at: datetime


class TenantConfigAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    changed_by_user_id: int
    user_email: str | None
    changed_at: datetime
    field_name: str
    old_value: str | None
    new_value: str | None
    ip_address: str | None


class TenantStatusResponse(BaseModel):
    is_open: bool
    estimated_prep_time_minutes: int
    message: str | None
    next_opening: str | None
    active_orders_count: int


class NextOpeningResponse(BaseModel):
    next_opening: str | None


class TenantClosureToggle(BaseModel):
    """Payload pour PATCH /tenant/toggle-closure (endpoint dédié rate-limité)."""

    is_temporarily_closed: bool
    temporary_closure_message: str | None = None

    @field_validator("temporary_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class TenantSuspendRequest(BaseModel):
    suspension_message: str = Field(..., min_length=1, max_length=500)


class TenantResponse(BaseModel):
    """Représentation d'un tenant pour les endpoints super-admin."""

    id: int
    slug: str
    name: str
    plan: str
    created_at: datetime
    is_suspended: bool
    suspended_at: datetime | None
    suspension_message: str | None
