"""Schémas Pydantic — module HACCP.

Conventions :
- *Create  : payload entrant (POST)
- *Update  : payload partiel (PATCH)
- *Response: réponse sortante (GET / POST response)
- HaccpStatusResponse : état du gate bloquant ouverture/fermeture
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Alias utilise uniquement par les champs nommes "date" (ex: HaccpSessionCreate.date) :
# `champ: date | None = None` masquerait le type `date` importe ci-dessus des que
# l'affectation `= None` lie le nom `date` dans le corps de la classe, avant meme
# l'evaluation de l'annotation -- et Pydantic re-evalue les annotations en incluant
# le namespace de la classe, donc une simple annotation en chaine ne suffit pas non plus.
_date = date


# ─── Equipment ───────────────────────────────────────────────────────────────

EquipmentType = Literal["fridge", "freezer", "cold_room", "hot_hold", "ambient"]


class HaccpEquipmentCreate(BaseModel):
    name: str = Field(..., max_length=128)
    type: EquipmentType
    location: str | None = Field(None, max_length=128)
    target_min_temp: float | None = None
    target_max_temp: float | None = None
    check_at_opening: bool = True
    check_at_closing: bool = True

    @field_validator("target_max_temp")
    @classmethod
    def validate_temp_range(cls, v: float | None, info) -> float | None:
        if v is not None and info.data.get("target_min_temp") is not None:
            if v <= info.data["target_min_temp"]:
                raise ValueError("target_max_temp doit être supérieur à target_min_temp.")
        return v


class HaccpEquipmentUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    type: EquipmentType | None = None
    location: str | None = None
    target_min_temp: float | None = None
    target_max_temp: float | None = None
    check_at_opening: bool | None = None
    check_at_closing: bool | None = None
    is_active: bool | None = None


class HaccpEquipmentResponse(BaseModel):
    id: int
    name: str
    type: str
    location: str | None
    target_min_temp: float | None
    target_max_temp: float | None
    check_at_opening: bool
    check_at_closing: bool
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Check Sessions ───────────────────────────────────────────────────────────

SessionType = Literal["opening", "closing"]
SessionStatus = Literal["in_progress", "complete", "incomplete_validated"]


class HaccpSessionCreate(BaseModel):
    session_type: SessionType
    date: _date | None = None  # défaut = today côté service


class HaccpSessionComplete(BaseModel):
    notes: str | None = None
    force: bool = False  # permet de valider même si des items manquent (incomplete_validated)


class HaccpSessionResponse(BaseModel):
    id: int
    session_type: str
    date: date
    started_by: int | None
    completed_by: int | None
    status: str
    completed_at: datetime | None
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Temperature Logs ─────────────────────────────────────────────────────────

class HaccpTemperatureCreate(BaseModel):
    equipment_id: int
    measured_temp: float
    corrective_action: str | None = None


class HaccpTemperatureResponse(BaseModel):
    id: int
    session_id: int
    equipment_id: int
    measured_temp: float
    is_compliant: bool
    corrective_action: str | None
    logged_by: int | None
    logged_at: datetime

    model_config = {"from_attributes": True}


# ─── DLC Checks ───────────────────────────────────────────────────────────────

DlcLevel = Literal[1, 2, 3]


class HaccpDlcCheckCreate(BaseModel):
    ingredient_id: int | None = None
    batch_id: int | None = None
    ingredient_name: str = Field(..., max_length=128)
    dlc_level: DlcLevel
    dlc_date: date
    location: str | None = Field(None, max_length=128)
    is_compliant: bool
    corrective_action: str | None = None


class HaccpDlcCheckResponse(BaseModel):
    id: int
    session_id: int
    ingredient_id: int | None
    batch_id: int | None
    ingredient_name: str
    dlc_level: int
    dlc_date: date
    location: str | None
    is_compliant: bool
    corrective_action: str | None
    logged_by: int | None
    logged_at: datetime

    model_config = {"from_attributes": True}


# ─── Cleaning Tasks ───────────────────────────────────────────────────────────

CleaningFrequency = Literal["daily", "weekly", "monthly", "per_service"]
CleaningSessionType = Literal["opening", "closing", "both"]


class HaccpCleaningTaskCreate(BaseModel):
    name: str = Field(..., max_length=128)
    zone: str = Field(..., max_length=64)
    frequency: CleaningFrequency
    session_type: CleaningSessionType = "both"
    product_used: str | None = Field(None, max_length=128)
    required_role: str = "staff"


class HaccpCleaningTaskUpdate(BaseModel):
    name: str | None = None
    zone: str | None = None
    frequency: CleaningFrequency | None = None
    session_type: CleaningSessionType | None = None
    product_used: str | None = None
    required_role: str | None = None
    is_active: bool | None = None


class HaccpCleaningTaskResponse(BaseModel):
    id: int
    name: str
    zone: str
    frequency: str
    session_type: str
    product_used: str | None
    required_role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class HaccpCleaningLogCreate(BaseModel):
    task_id: int
    notes: str | None = None
    is_compliant: bool = True


class HaccpCleaningLogResponse(BaseModel):
    id: int
    session_id: int
    task_id: int
    completed_by: int | None
    completed_at: datetime
    notes: str | None
    is_compliant: bool

    model_config = {"from_attributes": True}


# ─── Non-Conformities ─────────────────────────────────────────────────────────

NcSourceType = Literal["temperature", "dlc", "cleaning", "reception", "cooling", "other"]
NcStatus = Literal["open", "in_progress", "closed"]


class HaccpNonConformityCreate(BaseModel):
    session_id: int | None = None
    source_type: NcSourceType
    source_id: int | None = None
    description: str


class HaccpNonConformityUpdate(BaseModel):
    corrective_action: str | None = None
    status: NcStatus | None = None


class HaccpNonConformityResponse(BaseModel):
    id: int
    session_id: int | None
    source_type: str
    source_id: int | None
    description: str
    corrective_action: str | None
    validated_by: int | None
    validated_at: datetime | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Reception Controls ───────────────────────────────────────────────────────

class HaccpReceptionCreate(BaseModel):
    supplier_name: str = Field(..., max_length=128)
    delivery_date: date
    temperature_on_arrival: float | None = None
    packaging_ok: bool
    labeling_ok: bool
    dlc_ok: bool
    is_accepted: bool
    refusal_reason: str | None = None

    @field_validator("refusal_reason")
    @classmethod
    def refusal_required_if_rejected(cls, v: str | None, info) -> str | None:
        if info.data.get("is_accepted") is False and not v:
            raise ValueError("refusal_reason est obligatoire si is_accepted=false.")
        return v


class HaccpReceptionResponse(BaseModel):
    id: int
    supplier_name: str
    delivery_date: date
    temperature_on_arrival: float | None
    packaging_ok: bool
    labeling_ok: bool
    dlc_ok: bool
    is_accepted: bool
    refusal_reason: str | None
    logged_by: int | None
    logged_at: datetime

    model_config = {"from_attributes": True}


# ─── Cooling Logs ─────────────────────────────────────────────────────────────

class HaccpCoolingCreate(BaseModel):
    product_name: str = Field(..., max_length=128)
    quantity: str | None = None
    temp_start: float
    started_at: datetime


class HaccpCoolingUpdate(BaseModel):
    temp_at_90min: float | None = None
    temp_final: float | None = None
    ended_at: datetime | None = None
    corrective_action: str | None = None


class HaccpCoolingResponse(BaseModel):
    id: int
    product_name: str
    quantity: str | None
    temp_start: float
    temp_at_90min: float | None
    temp_final: float | None
    started_at: datetime
    ended_at: datetime | None
    is_compliant: bool | None
    corrective_action: str | None
    logged_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Training Records ─────────────────────────────────────────────────────────

TrainingType = Literal["hygiene_14h", "refresher", "haccp_module", "other"]


class HaccpTrainingCreate(BaseModel):
    user_id: int
    user_name: str | None = None
    training_type: TrainingType
    training_date: date
    expiry_date: date | None = None
    provider: str | None = None
    certificate_ref: str | None = None


class HaccpTrainingResponse(BaseModel):
    id: int
    user_id: int
    user_name: str | None
    training_type: str
    training_date: date
    expiry_date: date | None
    provider: str | None
    certificate_ref: str | None
    logged_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Frying Oil Logs ─────────────────────────────────────────────────────────

OilAction = Literal["none", "filtered", "replaced"]


class HaccpFryingOilCreate(BaseModel):
    fryer_name: str = Field(..., max_length=64)
    polarity_percent: float | None = None
    color_ok: bool | None = None
    odor_ok: bool | None = None
    action_taken: OilAction | None = None

    @field_validator("polarity_percent")
    @classmethod
    def validate_polarity(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("polarity_percent doit être compris entre 0 et 100.")
        return v


class HaccpFryingOilResponse(BaseModel):
    id: int
    session_id: int
    fryer_name: str
    polarity_percent: float | None
    color_ok: bool | None
    odor_ok: bool | None
    is_compliant: bool
    action_taken: str | None
    logged_by: int | None
    logged_at: datetime

    model_config = {"from_attributes": True}


# ─── Status (gate bloquant) ───────────────────────────────────────────────────

class HaccpSessionSummary(BaseModel):
    """Résumé d'une session pour le status du jour."""

    session_id: int | None
    status: str  # "not_started" | "in_progress" | "complete" | "incomplete_validated"
    temperatures_done: int
    temperatures_total: int
    dlc_done: int
    cleaning_done: int
    cleaning_total: int
    has_non_conformities: bool


class HaccpStatusResponse(BaseModel):
    """État HACCP du jour — utilisé par le gate bloquant.

    ``can_open`` : True si la session d'ouverture est complète.
    ``can_close`` : True si la session de fermeture est complète.
    """

    today: date
    opening: HaccpSessionSummary
    closing: HaccpSessionSummary
    can_open: bool
    can_close: bool
    open_non_conformities: int
