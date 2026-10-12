"""Server-controlled daily workflow; clients never supply dates or compliance."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartSession(StrictRequest):
    session_type: Literal["opening", "closing"]


class RevisionRequest(StrictRequest):
    expected_revision: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"\S")


class EvidenceValue(StrictRequest):
    requirement_id: int = Field(gt=0)
    temperature: float | None = Field(
        default=None, allow_inf_nan=False, ge=-100, le=300
    )
    confirmed: bool | None = None
    corrective_action: str | None = Field(default=None, max_length=2000)


class RecordEvidence(RevisionRequest):
    items: list[EvidenceValue] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_items(self):
        if len({item.requirement_id for item in self.items}) != len(self.items):
            raise ValueError("Une exigence ne peut apparaître qu'une fois")
        return self


class ExplainRequest(RevisionRequest):
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    actions_taken: str = Field(min_length=1, max_length=4000, pattern=r"\S")


PositiveId = Annotated[int, Field(gt=0)]
