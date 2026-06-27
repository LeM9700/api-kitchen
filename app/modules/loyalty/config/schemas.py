from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LoyaltyConfigUpdate(BaseModel):
    """PATCH payload for tenant loyalty configuration."""

    base_ratio: Decimal | None = Field(None, gt=0, description="Points earned per euro spent")
    points_expiry_days: int | None = Field(None, ge=1, description="Days before expiry; null disables expiry")
    points_to_euro_rate: Decimal | None = Field(None, gt=0, description="Euro value of one point")
    max_cumulative_multiplier: Decimal | None = Field(None, ge=Decimal("1.0"), le=Decimal("20.0"))
    is_active: bool | None = None


class LoyaltyConfigResponse(BaseModel):
    """GET/PATCH response for tenant loyalty configuration."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    base_ratio: Decimal
    points_expiry_days: int | None
    points_to_euro_rate: Decimal
    max_cumulative_multiplier: Decimal
    is_active: bool
    updated_at: datetime


class LoyaltyRuleCreate(BaseModel):
    """POST payload for a loyalty bonus rule."""

    name: str = Field(..., max_length=128)
    rule_type: str = Field(..., pattern="^(category_multiplier|period_multiplier|day_multiplier|first_order)$")
    category_id: int | None = None
    multiplier: Decimal = Field(..., ge=Decimal("1.0"), le=Decimal("10.0"))
    start_date: date | None = None
    end_date: date | None = None
    days_of_week: list[int] | None = Field(None, description="0=Monday, 6=Sunday")
    priority: int = Field(0, ge=0, le=100)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_rule_shape(self):
        if self.rule_type == "category_multiplier" and self.category_id is None:
            raise ValueError("category_id is required for category_multiplier")
        if self.rule_type == "day_multiplier":
            if not self.days_of_week:
                raise ValueError("days_of_week is required for day_multiplier")
            if any(day < 0 or day > 6 for day in self.days_of_week):
                raise ValueError("days_of_week values must be between 0 and 6")
        if self.rule_type == "period_multiplier":
            if self.start_date is None or self.end_date is None:
                raise ValueError("start_date and end_date are required for period_multiplier")
            if self.end_date < self.start_date:
                raise ValueError("end_date must be greater than or equal to start_date")
        return self


class LoyaltyRuleUpdate(BaseModel):
    """PATCH payload for a loyalty bonus rule."""

    name: str | None = Field(None, max_length=128)
    multiplier: Decimal | None = Field(None, ge=Decimal("1.0"), le=Decimal("10.0"))
    start_date: date | None = None
    end_date: date | None = None
    days_of_week: list[int] | None = None
    priority: int | None = Field(None, ge=0, le=100)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_update_shape(self):
        if self.days_of_week is not None and any(day < 0 or day > 6 for day in self.days_of_week):
            raise ValueError("days_of_week values must be between 0 and 6")
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        return self


class LoyaltyRuleResponse(BaseModel):
    """Response for a loyalty bonus rule."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    rule_type: str
    category_id: int | None
    multiplier: Decimal
    start_date: date | None
    end_date: date | None
    days_of_week: list[int] | None
    priority: int
    is_active: bool
    created_at: datetime


class LoyaltyRewardCreate(BaseModel):
    """POST payload for a redeemable reward."""

    name: str = Field(..., max_length=128)
    reward_type: str = Field(..., pattern="^(discount_euros|free_product)$")
    points_required: int = Field(..., ge=1)
    discount_amount: Decimal | None = Field(None, gt=0)
    product_id: int | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_reward_shape(self):
        if self.reward_type == "discount_euros" and self.discount_amount is None:
            raise ValueError("discount_amount is required for discount_euros")
        if self.reward_type == "free_product" and self.product_id is None:
            raise ValueError("product_id is required for free_product")
        return self


class LoyaltyRewardUpdate(BaseModel):
    """PATCH payload for a redeemable reward."""

    name: str | None = Field(None, max_length=128)
    points_required: int | None = Field(None, ge=1)
    discount_amount: Decimal | None = Field(None, gt=0)
    product_id: int | None = None
    is_active: bool | None = None


class LoyaltyRewardResponse(BaseModel):
    """Response for a redeemable reward."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    reward_type: str
    points_required: int
    discount_amount: Decimal | None
    product_id: int | None
    is_active: bool
    created_at: datetime
    can_redeem: bool | None = None
    missing_points: int | None = None


class LoyaltyRewardEligibilityResponse(LoyaltyRewardResponse):
    can_redeem: bool
    missing_points: int


class LoyaltyPointsPreview(BaseModel):
    """Preview of points earned by an order before checkout."""

    base_points: int = Field(..., description="Base points without bonus")
    bonus_points: int = Field(..., description="Bonus points from active rules")
    total_points: int = Field(..., description="base_points + bonus_points")
    applied_rules: list[str] = Field(..., description="Applied bonus rule names")
    total_multiplier: Decimal = Decimal("1.0")
    max_multiplier: Decimal = Decimal("20.0")
    multiplier_was_capped: bool = False


class RedeemResponse(BaseModel):
    """Result of a reward redemption.

    Attributes:
        discount_euros: Montant de la réduction (uniquement si reward_type='discount_euros').
        free_product_id: ID du produit offert (uniquement si reward_type='free_product').
        remaining_points: Solde de points restant après débit.
        promo_code: Code promo à usage unique généré automatiquement pour les récompenses
            de type 'discount_euros'. À passer dans le champ ``promo_code`` lors de la
            création d'une commande. None pour les récompenses de type 'free_product'.
    """

    discount_euros: float | None = None
    free_product_id: int | None = None
    remaining_points: int
    promo_code: str | None = None


class LoyaltyStatsTopReward(BaseModel):
    reward_id: int
    redemptions: int
    points_redeemed: int


class LoyaltyStatsResponse(BaseModel):
    member_count: int
    active_member_count: int
    points_distributed: int
    points_redeemed: int
    points_expired: int
    circulating_balance: int
    redemption_rate: float
    transaction_counts_by_type: dict[str, int]
    top_rewards: list[LoyaltyStatsTopReward]
