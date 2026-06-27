from pydantic import BaseModel, ConfigDict


class DeliveryZoneCreate(BaseModel):
    name: str
    polygon: dict
    fee: float
    min_order_amount: float = 0
    estimated_minutes: int = 30


class DeliveryZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    fee: float
    min_order_amount: float
    estimated_minutes: int
    is_active: bool


class AddressCheckRequest(BaseModel):
    lat: float
    lng: float
