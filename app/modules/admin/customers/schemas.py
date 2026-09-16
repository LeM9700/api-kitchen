from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.modules.orders.schemas import OrderDetailOut, OrderListOut
from app.modules.payments.schemas import PaymentDetailOut

CommunicationChannel = Literal["email", "push"]
CommunicationType = Literal["transactional", "marketing"]


class AdminCustomerListItem(BaseModel):
    id: int
    email: EmailStr | str
    full_name: str | None = None
    phone: str | None = None
    is_active: bool
    email_verified: bool
    marketing_email_opt_in: bool = False
    marketing_push_opt_in: bool = False
    order_count: int = 0
    total_spent: float = 0
    last_order_at: datetime | None = None
    loyalty_points: int = 0
    created_at: datetime | None = None


class AdminCustomerDetail(AdminCustomerListItem):
    orders: list[OrderListOut] = Field(default_factory=list)


class AdminCustomerOrderDetail(BaseModel):
    order: OrderDetailOut
    payment: PaymentDetailOut | None = None


class CustomerCommunicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    channel: str
    message_type: str
    template_key: str | None = None
    subject: str | None = None
    body: str
    status: str
    error: str | None = None
    sent_by_user_id: int | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None


class MessageTemplateOut(BaseModel):
    key: str
    label: str
    message_type: CommunicationType
    channels: list[CommunicationChannel]
    subject: str | None = None
    body: str


class CustomerMessageSendRequest(BaseModel):
    channels: list[CommunicationChannel] = Field(min_length=1, max_length=2)
    message_type: CommunicationType = "transactional"
    template_key: str | None = None
    subject: str | None = Field(None, max_length=255)
    body: str | None = Field(None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _require_body_or_template(self) -> "CustomerMessageSendRequest":
        if not self.template_key and not self.body:
            raise ValueError("template_key ou body est requis")
        if "email" in self.channels and not self.subject and not self.template_key:
            raise ValueError("subject est requis pour un email sans template")
        return self


class BulkCustomerMessageRequest(CustomerMessageSendRequest):
    customer_ids: list[int] | None = Field(None, min_length=1)
    query: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    email_verified: bool | None = None
    marketing_email_opt_in: bool | None = None
    marketing_push_opt_in: bool | None = None
    confirm_bulk_send: bool = False

    @model_validator(mode="after")
    def _require_scope_and_confirmation(self) -> "BulkCustomerMessageRequest":
        if not self.customer_ids and not any(
            value is not None
            for value in (
                self.query,
                self.is_active,
                self.email_verified,
                self.marketing_email_opt_in,
                self.marketing_push_opt_in,
            )
        ):
            raise ValueError("customer_ids ou au moins un filtre est requis")
        if not self.confirm_bulk_send:
            raise ValueError("confirm_bulk_send doit etre true pour un envoi groupe")
        return self


class CustomerMessageSendResult(BaseModel):
    created: int
    skipped: int
    communication_ids: list[int] = Field(default_factory=list)


class CustomerPrivacyActionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=512)


class CustomerPrivacyExport(BaseModel):
    profile: AdminCustomerListItem
    orders: list[OrderDetailOut]
    communications: list[CustomerCommunicationOut]
    exported_at: datetime


class AdminAuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_user_id: int | None = None
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: str
    metadata_json: dict[str, Any] | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None
