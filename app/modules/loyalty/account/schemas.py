from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
