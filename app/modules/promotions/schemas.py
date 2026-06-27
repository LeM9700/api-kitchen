from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DiscountType = Literal["fixed", "percent"]


class PromotionTargets(BaseModel):
    category_ids: list[int] = Field(default_factory=list)
    product_ids: list[int] = Field(default_factory=list)


class PromotionCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(None, max_length=255)
    discount_type: DiscountType
    discount_value: float = Field(..., gt=0)
    min_order_amount: float = Field(0, ge=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True
    max_uses: int | None = Field(None, gt=0)
    max_uses_per_user: int | None = Field(None, gt=0)
    first_order_only: bool = False
    campaign_id: int | None = None
    user_id: int | None = None
    is_public: bool = True
    is_stackable: bool = False
    email_verified_required: bool = False
    target_category_ids: list[int] = Field(default_factory=list)
    target_product_ids: list[int] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_dates(self) -> "PromotionCreate":
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class PromotionUpdate(BaseModel):
    code: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = Field(None, max_length=255)
    discount_type: DiscountType | None = None
    discount_value: float | None = Field(None, gt=0)
    min_order_amount: float | None = Field(None, ge=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None
    max_uses: int | None = Field(None, gt=0)
    max_uses_per_user: int | None = Field(None, gt=0)
    first_order_only: bool | None = None
    campaign_id: int | None = None
    user_id: int | None = None
    is_public: bool | None = None
    is_stackable: bool | None = None
    email_verified_required: bool | None = None
    target_category_ids: list[int] | None = None
    target_product_ids: list[int] | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None


class PromotionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str | None = None
    discount_type: DiscountType
    discount_value: float
    min_order_amount: float = 0
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool
    max_uses: int | None = None
    max_uses_per_user: int | None = None
    current_uses: int = 0
    first_order_only: bool = False
    campaign_id: int | None = None
    user_id: int | None = None
    is_public: bool = True
    is_stackable: bool = False
    email_verified_required: bool = False
    targets: PromotionTargets = Field(default_factory=PromotionTargets)


class PromotionPublicOut(BaseModel):
    id: int
    code: str
    description: str | None = None
    discount_type: DiscountType
    discount_value: float
    min_order_amount: float = 0
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    targets: PromotionTargets = Field(default_factory=PromotionTargets)


class PromotionAdminOut(PromotionOut):
    usage_count: int = 0
    unique_users: int = 0
    remaining_uses: int | None = None
    revenue_gross: float = 0
    revenue_net: float = 0
    discount_total: float = 0


class PromotionCartItem(BaseModel):
    product_id: int | None = None
    category_id: int | None = None
    quantity: int = Field(1, gt=0)
    unit_price: float = Field(0, ge=0)
    line_total: float | None = Field(None, ge=0)

    @property
    def total(self) -> float:
        if self.line_total is not None:
            return float(self.line_total)
        return float(self.quantity) * float(self.unit_price)


class PromotionValidateRequest(BaseModel):
    code: str | None = Field(None, min_length=1, max_length=64)
    codes: list[str] | None = None
    order_total: float | None = Field(None, ge=0)
    items: list[PromotionCartItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_payload(self) -> "PromotionValidateRequest":
        if self.code is None and not self.codes:
            raise ValueError("code or codes is required")
        if not self.items and self.order_total is None:
            raise ValueError("items or order_total is required")
        return self

    def normalized_codes(self) -> list[str]:
        raw_codes = self.codes if self.codes is not None else [self.code]
        return [str(code).strip().upper() for code in raw_codes if str(code).strip()]


ValidateRequest = PromotionValidateRequest


class PromotionValidateOut(BaseModel):
    valid: bool = True
    discount: float
    discounts: dict[str, float] = Field(default_factory=dict)
    promo_id: int | None = None
    promo_ids: list[int] = Field(default_factory=list)


class PromotionUsageOut(BaseModel):
    id: int
    promo_code_id: int
    user_id: int
    order_id: int
    used_at: datetime
    order_status: str | None = None
    subtotal: float = 0
    discount_total: float = 0
    total: float = 0


class PromotionCampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    prefix: str = Field(..., min_length=1, max_length=32)
    description: str | None = Field(None, max_length=255)
    count: int = Field(..., ge=1, le=1000)
    promotion: PromotionCreate

    @field_validator("prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        return value.strip().upper()


class PromotionCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prefix: str
    description: str | None = None
    created_by_user_id: int | None = None
    created_at: datetime
    codes: list[str] = Field(default_factory=list)


class PromotionToggleRequest(BaseModel):
    is_active: bool


class PromotionCleanupOut(BaseModel):
    expired_count: int
    promotion_ids: list[int]


class PromotionSuperAdminStatsOut(BaseModel):
    tenant_slug: str
    usage_count: int = 0
    unique_users: int = 0
    revenue_gross: float = 0
    revenue_net: float = 0
    discount_total: float = 0
