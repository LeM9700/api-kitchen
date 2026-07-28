from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.payments.schemas import PaymentOut

OrderType = Literal["delivery", "pickup", "dine_in"]
PreparationStatus = Literal["pending", "preparing", "ready"]
PreparationStation = Literal["kitchen", "counter", "none"]
ManualPaymentMethod = Literal["cash", "external_terminal", "cash_register"]


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
    order_type: OrderType = "delivery"
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

    @model_validator(mode="after")
    def _validate_delivery_fields(self) -> "OrderCreate":
        if self.order_type == "delivery" and not self.delivery_address:
            raise ValueError("delivery_address est requis pour une commande en livraison")
        return self


class ManualOrderCustomer(BaseModel):
    email: str | None = None
    full_name: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=32)


class ManualOrderPaymentCreate(BaseModel):
    method: ManualPaymentMethod
    external_reference: str | None = Field(None, max_length=255)
    amount_received: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _validate_reference(self) -> "ManualOrderPaymentCreate":
        if self.method in {"external_terminal", "cash_register"} and not self.external_reference:
            raise ValueError("external_reference est requis pour ce mode de paiement")
        return self


class ManualOrderCreate(OrderCreate):
    customer: ManualOrderCustomer | None = None
    table_number: str | None = Field(None, max_length=32)
    loyalty_user_id: int | None = Field(None, ge=1)
    loyalty_points_to_use: int | None = Field(None, ge=1)
    payment: ManualOrderPaymentCreate
    note: str | None = Field(None, max_length=512)

    @model_validator(mode="after")
    def _copy_customer_fields(self) -> "ManualOrderCreate":
        if self.customer is not None:
            self.customer_email = self.customer.email
        if self.loyalty_points_to_use is not None and self.loyalty_user_id is None:
            raise ValueError("loyalty_user_id est requis avec loyalty_points_to_use")
        return self


class OrderStatusUpdate(BaseModel):
    status: str
    note: str | None = None


class OrderItemPreparationUpdate(BaseModel):
    status: PreparationStatus
    note: str | None = Field(None, max_length=512)


class OrderListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_email: str | None
    customer_name: str | None = None
    customer_phone: str | None = None
    order_type: str = "delivery"
    status: str
    payment_status: str = "pending"
    source: str = "customer"
    created_by_user_id: int | None = None
    subtotal: float
    discount_total: float
    delivery_fee: float
    total: float
    delivery_address: str | None
    delivery_zone_id: int | None = None
    table_number: str | None = None
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
    preparation_status: PreparationStatus = "pending"
    preparation_station: PreparationStation = "kitchen"
    prepared_at: datetime | None = None
    prepared_by_user_id: int | None = None


class OrderStationSummaryOut(BaseModel):
    station: PreparationStation
    total_items: int
    ready_items: int
    all_ready: bool


class OrderStatusHistoryOut(BaseModel):
    status: str
    note: str | None = None
    created_at: datetime | None = None


class OrderDetailOut(OrderListOut):
    user_id: int | None = None
    promo_code: str | None = None
    items: list[OrderItemOut] = Field(default_factory=list)
    station_summary: list[OrderStationSummaryOut] = Field(default_factory=list)
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
    customer_name: str | None = None
    delivery_address: str | None = None
    table_number: str | None = None
    created_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    items: list[ReceiptItemOut]
    totals: dict[str, float]
    meta: dict[str, Any] = Field(default_factory=dict)


class ManualOrderOut(BaseModel):
    order: OrderDetailOut
    payment: PaymentOut
    receipt: OrderReceiptOut


OrderOut = OrderListOut
