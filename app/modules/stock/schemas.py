from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BatchStatus = Literal["sealed", "opened", "expired", "consumed", "discarded"]
AdjustmentRequestStatus = Literal["pending", "approved", "rejected"]


class IngredientCreate(BaseModel):
    name: str
    unit: str
    current_qty: float = 0
    alert_threshold: float = 0
    purchase_price_per_unit: float | None = Field(default=None, ge=0)
    purchase_unit: str | None = None


class IngredientOut(IngredientCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_below_threshold: bool


class SupplyRequest(BaseModel):
    ingredient_id: int
    quantity: float = Field(gt=0, description="Must be strictly positive")


class ProductIngredientCreate(BaseModel):
    product_id: int = Field(gt=0)
    ingredient_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


class VariantIngredientCreate(BaseModel):
    variant_id: int = Field(gt=0)
    ingredient_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


class ExtraIngredientCreate(BaseModel):
    extra_id: int = Field(gt=0)
    ingredient_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


class StockRecipeLineCreate(BaseModel):
    ingredient_id: int = Field(gt=0)
    quantity: float = Field(gt=0)
    unit: str | None = None


class StockRecipeReplace(BaseModel):
    items: list[StockRecipeLineCreate] = Field(default_factory=list)


class StockRecipeLineOut(BaseModel):
    id: int
    recipe_type: Literal["product", "variant", "extra"]
    target_id: int
    ingredient_id: int
    ingredient_name: str | None = None
    quantity: float
    unit: str | None = None


class StockRecipeOut(BaseModel):
    recipe_type: Literal["product", "variant", "extra"]
    target_id: int
    items: list[StockRecipeLineOut]


class MissingStockRecipeOut(BaseModel):
    recipe_type: Literal["product", "variant", "extra"]
    target_id: int
    name: str
    product_id: int | None = None


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
    purchase_price_per_unit: float | None = Field(default=None, ge=0)
    purchase_unit: str | None = None


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


class IngredientBatchCreate(BaseModel):
    quantity: float = Field(gt=0)
    received_at: datetime | None = None
    expires_at: datetime | None = None
    use_within_hours_after_opening: int | None = Field(None, ge=1, le=8760)


class IngredientBatchPatch(BaseModel):
    quantity: float | None = Field(None, gt=0)
    expires_at: datetime | None = None
    opened_at: datetime | None = None
    use_within_hours_after_opening: int | None = Field(None, ge=1, le=8760)
    status: BatchStatus | None = None


class IngredientBatchDiscardRequest(BaseModel):
    reason: str = Field("batch_discard", max_length=64)


class IngredientBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ingredient_id: int
    quantity: float
    received_at: datetime
    expires_at: datetime | None = None
    opened_at: datetime | None = None
    use_within_hours_after_opening: int | None = None
    effective_expires_at: datetime | None = None
    status: BatchStatus
    created_by_user_id: int | None = None
    created_at: datetime | None = None


class StockAdjustmentRequestCreate(BaseModel):
    ingredient_id: int
    quantity_delta: float
    reason: Literal["waste", "loss", "correction", "inventory"]
    note: str | None = Field(None, max_length=512)


class StockAdjustmentReviewRequest(BaseModel):
    note: str | None = Field(None, max_length=512)


class StockAdjustmentRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ingredient_id: int
    quantity_delta: float
    reason: str
    note: str | None = None
    status: AdjustmentRequestStatus
    requested_by_user_id: int
    reviewed_by_user_id: int | None = None
    reviewed_at: datetime | None = None
    is_large_adjustment: bool = False
    created_at: datetime | None = None
