from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KdsScreenMode = Literal["kitchen", "counter", "service"]
KdsInteractionMode = Literal["wall", "touch"]
KDS_SCREEN_PATCH_NON_NULL_FIELDS = frozenset(
    {
        "name",
        "screen_key",
        "mode",
        "station",
        "interaction_mode",
        "tickets_per_page",
        "is_active",
    }
)


class KdsScreenBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    screen_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$",
    )
    mode: KdsScreenMode = "kitchen"
    station: str = Field("kitchen", min_length=1, max_length=64)
    interaction_mode: KdsInteractionMode = "wall"
    tickets_per_page: int = Field(4, ge=1, le=8)
    is_active: bool = True

    @field_validator("name", "screen_key", "station", mode="before")
    @classmethod
    def strip_non_empty_strings(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value


class KdsScreenCreate(KdsScreenBase):
    pass


class KdsScreenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    screen_key: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$",
    )
    mode: KdsScreenMode | None = None
    station: str | None = Field(None, min_length=1, max_length=64)
    interaction_mode: KdsInteractionMode | None = None
    tickets_per_page: int | None = Field(None, ge=1, le=8)
    is_active: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_values(cls, data: object) -> object:
        if isinstance(data, dict):
            null_fields = sorted(
                field
                for field in KDS_SCREEN_PATCH_NON_NULL_FIELDS
                if field in data and data[field] is None
            )
            if null_fields:
                raise ValueError(f"KDS screen patch fields cannot be null: {', '.join(null_fields)}")
        return data

    @field_validator("name", "screen_key", "station", mode="before")
    @classmethod
    def strip_non_empty_strings(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            return value.strip()
        return value


class KdsScreenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    screen_key: str
    mode: KdsScreenMode
    station: str
    interaction_mode: KdsInteractionMode
    tickets_per_page: int
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KdsPairingCodeOut(BaseModel):
    screen_id: int
    code: str = Field(..., pattern=r"^\d{6}$")
    expires_at: datetime


class KdsPairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., pattern=r"^\d{6}$")
    device_label: str | None = Field(None, min_length=1, max_length=128)

    @field_validator("code", "device_label", mode="before")
    @classmethod
    def strip_strings(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            return value.strip()
        return value


class KdsRemoteSessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    screen: KdsScreenOut


class KdsRemoteSessionStatusOut(BaseModel):
    id: int
    screen_id: int
    paired_by_user_id: int | None = None
    device_label: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    expires_at: datetime
    screen: KdsScreenOut


class KdsRemoteSessionRevokeOut(BaseModel):
    revoked: bool


class KdsScreenSessionsRevokedOut(BaseModel):
    revoked_count: int
