from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrderItemExtraCreate(BaseModel):
    extra_id: int
    quantity: int = Field(1, ge=1, le=20)


class OrderItemCreate(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int = Field(..., ge=1, le=99)
    extras: list[OrderItemExtraCreate] = Field(default_factory=list)
    # [🔒 SÉCURITÉ] unit_price retiré du payload client — le prix est lu
    # exclusivement depuis le catalogue côté serveur (orders/service.py::create_order).


class OrderCreate(BaseModel):
    customer_email: str | None = None
    delivery_address: str | None = None
    delivery_zone_id: int | None = None
    delivery_fee: float = 0
    # [🔒 SÉCURITÉ] discount_total est ignoré côté serveur — le calcul se fait
    # exclusivement depuis promo_code. Ce champ est conservé pour rétrocompatibilité
    # des clients existants mais n'a aucun effet sur la commande créée.
    discount_total: float = 0
    promo_code: str | None = None
    items: list[OrderItemCreate] = Field(..., min_length=1)


class OrderStatusUpdate(BaseModel):
    status: str
    note: str | None = None


class OrderListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_email: str | None
    status: str
    payment_status: str = "pending"
    subtotal: float
    discount_total: float
    delivery_fee: float
    total: float
    delivery_address: str | None
    delivery_zone_id: int | None = None
    estimated_delivery_at: datetime | None = None
    created_at: datetime | None = None


class OrderItemExtraOut(BaseModel):
    extra_id: int
    name: str
    quantity: int
    unit_price: float
    total: float


class OrderItemOut(BaseModel):
    id: int
    product_id: int
    variant_id: int | None = None
    product_name: str | None = None
    variant_name: str | None = None
    quantity: int
    unit_price: float
    extras_total: float = 0
    total: float
    extras: list[OrderItemExtraOut] = Field(default_factory=list)


class OrderStatusHistoryOut(BaseModel):
    status: str
    note: str | None = None
    created_at: datetime | None = None


class OrderDetailOut(OrderListOut):
    user_id: int | None = None
    promo_code: str | None = None
    items: list[OrderItemOut] = Field(default_factory=list)
    status_history: list[OrderStatusHistoryOut] = Field(default_factory=list)


class ReorderItemOut(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int
    extras: list[OrderItemExtraCreate] = Field(default_factory=list)
    available: bool = True
    warning: str | None = None


class ReorderOut(BaseModel):
    source_order_id: int
    items: list[ReorderItemOut]
    unavailable_items: list[ReorderItemOut] = Field(default_factory=list)


class ReceiptItemOut(BaseModel):
    label: str
    quantity: int
    unit_price: float
    extras: list[OrderItemExtraOut] = Field(default_factory=list)
    total: float


class OrderReceiptOut(BaseModel):
    order_id: int
    status: str
    payment_status: str
    customer_email: str | None = None
    delivery_address: str | None = None
    created_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    items: list[ReceiptItemOut]
    totals: dict[str, float]
    meta: dict[str, Any] = Field(default_factory=dict)


OrderOut = OrderListOut
