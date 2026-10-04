"""Phase-1 contracts only: no route, activation or authorization decision here."""

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HaccpBlockReason(StrEnum):
    NOT_CONFIGURED = "HACCP_NOT_CONFIGURED"
    STOCK_UNRECONCILED = "HACCP_STOCK_UNRECONCILED"
    OPENING_MISSING = "HACCP_OPENING_MISSING"
    PREVIOUS_CLOSING_MISSING = "HACCP_PREVIOUS_CLOSING_MISSING"
    DLC_CONFIRMATION_MISSING = "HACCP_DLC_CONFIRMATION_MISSING"
    DLC_MISSING = "HACCP_DLC_MISSING"
    EXPIRED_BATCH_NOT_EXCLUDED = "HACCP_EXPIRED_BATCH_NOT_EXCLUDED"
    TEMPERATURE_MISSING = "HACCP_TEMPERATURE_MISSING"
    TEMPERATURE_NONCOMPLIANT = "HACCP_TEMPERATURE_NONCOMPLIANT"
    CLEANING_MISSING = "HACCP_CLEANING_MISSING"
    CLEANING_NONCOMPLIANT = "HACCP_CLEANING_NONCOMPLIANT"
    EVIDENCE_STALE = "HACCP_EVIDENCE_STALE"
    DAY_CLOSED = "HACCP_DAY_CLOSED"
    EVALUATION_UNAVAILABLE = "HACCP_EVALUATION_UNAVAILABLE"


class ChecklistConfirmation(BaseModel):
    """Actor/time/compliance are resolved by the server, never accepted from UI."""

    model_config = ConfigDict(extra="forbid")
    establishment_id: int = Field(gt=0)
    session_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    requirement_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"\S")

    @field_validator("requirement_ids")
    @classmethod
    def unique_requirements(cls, values: list[int]) -> list[int]:
        if len(set(values)) != len(values):
            raise ValueError("Requirements must not be repeated")
        return values


class ClosureRegularizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    establishment_id: int = Field(gt=0)
    closing_session_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    actions_taken: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"\S")


class EstablishmentReadiness(BaseModel):
    """Read-only migration diagnostic. Not a public restaurant opening status."""

    establishment_id: int | None
    reference_date: date
    strict_enabled: bool = False
    activation_available: bool = False  # Phase 1 never enables strict mode.
    blockers: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
