from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.loyalty.config.schemas import LoyaltyRewardEligibilityResponse


class LoyaltyAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    points: int
    point_value_euros: Decimal = Decimal("0")
    expiring_soon_points: int = 0


class PointsRequest(BaseModel):
    user_id: int
    points: int = Field(..., gt=0)
    reason: str = "manual"


class RedeemPointsRequest(BaseModel):
    points: int = Field(..., gt=0)
    reason: str = "redeem"


class LoyaltyTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    points_delta: int
    reason: str
    transaction_type: str
    source: str
    changed_by_user_id: int | None = None
    order_id: int | None = None
    reward_id: int | None = None
    reservation_id: int | None = None
    metadata_json: dict | None = None
    created_at: datetime


class LoyaltyTransactionPage(BaseModel):
    items: list[LoyaltyTransactionOut]
    page: int
    limit: int
    total: int


class CheckoutReservationCreate(BaseModel):
    order_id: int = Field(..., ge=1)
    points_to_use: int = Field(..., gt=0)


class CheckoutReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    order_id: int
    points_reserved: int
    discount_amount: Decimal
    status: str
    expires_at: datetime
    created_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None


class ExpiringPointsBucket(BaseModel):
    days_until_expiry: int
    points: int


class ExpiringPointsResponse(BaseModel):
    points_expiry_days: int | None
    total_expiring_points: int
    buckets: list[ExpiringPointsBucket]


class LoyaltyStaffCustomerOut(BaseModel):
    id: int
    full_name: str | None = None
    masked_phone: str | None = None
    phone_last4: str | None = None
    points: int = 0
    available_points: int = 0
    phone_verified: bool = False
    pending_profile_completion: bool = False


class LoyaltyStaffCustomerSearchResponse(BaseModel):
    items: list[LoyaltyStaffCustomerOut]
    min_digits: int = 4


class LoyaltyQrTokenResponse(BaseModel):
    token: str
    expires_at: datetime
    ttl_seconds: int


class LoyaltyQrIdentifyRequest(BaseModel):
    token: str = Field(..., min_length=24, max_length=2048)


class LoyaltyStaffCustomerCreateRequest(BaseModel):
    phone: str = Field(..., min_length=4, max_length=32)
    first_name: str = Field(..., min_length=1, max_length=80)
    last_name: str = Field(..., min_length=1, max_length=80)

    @property
    def full_name(self) -> str:
        return f"{self.first_name.strip()} {self.last_name.strip()}".strip()


class LoyaltyStaffCustomerWalletOut(BaseModel):
    customer: LoyaltyStaffCustomerOut
    rewards: list[LoyaltyRewardEligibilityResponse]
