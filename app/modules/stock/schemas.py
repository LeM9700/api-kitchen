from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngredientCreate(BaseModel):
    name: str
    unit: str
    current_qty: float = 0
    alert_threshold: float = 0


class IngredientOut(IngredientCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_below_threshold: bool


class SupplyRequest(BaseModel):
    ingredient_id: int
    quantity: float = Field(gt=0, description="Must be strictly positive")


class ProductIngredientCreate(BaseModel):
    product_id: int
    ingredient_id: int
    quantity: float


class VariantIngredientCreate(BaseModel):
    variant_id: int
    ingredient_id: int
    quantity: float


class ExtraIngredientCreate(BaseModel):
    extra_id: int
    ingredient_id: int
    quantity: float


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ingredient_id: int
    quantity_delta: float
    reason: str
    user_id: int | None
    created_at: datetime


class IngredientPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    unit: str | None = None
    alert_threshold: float | None = None


class IngredientAdjustRequest(BaseModel):
    reason: Literal["inventory", "waste", "correction"]
    quantity: float | None = None
    new_qty: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_payload(self) -> "IngredientAdjustRequest":
        if self.reason == "inventory":
            if self.new_qty is None:
                raise ValueError("new_qty is required when reason=inventory")
            return self

        if self.quantity is None:
            raise ValueError("quantity is required for non-inventory adjustments")
        return self
