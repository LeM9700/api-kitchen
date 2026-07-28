from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PaymentIntentRequest(BaseModel):
    order_id: int


class LocalTestPaymentRequest(BaseModel):
    order_id: int


class PaymentConfirmRequest(BaseModel):
    provider_payment_id: str


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    provider: str
    provider_payment_id: str | None
    external_reference: str | None = None
    amount: float
    amount_received: float | None = None
    currency: str
    status: str
    created_by_user_id: int | None = None
    receipt_url: str | None = None


class TerminalConnectionTokenOut(BaseModel):
    secret: str


class TerminalPaymentIntentRequest(BaseModel):
    order_id: int
    reader_id: str | None = None
    process_on_reader: bool = False


class TerminalPaymentIntentOut(BaseModel):
    client_secret: str | None = None
    payment: PaymentOut
    reader_action: dict | None = None


class TerminalReaderListOut(BaseModel):
    readers: list[dict]


class TerminalReaderActionOut(BaseModel):
    reader: dict


class RefundCreate(BaseModel):
    """Corps de la requête de remboursement.

    Attributes:
        amount: Montant en centimes à rembourser. ``None`` déclenche un remboursement total.
        reason: Motif libre transmis pour audit (optionnel, max 256 chars).
    """

    amount: int | None = Field(None, gt=0)
    reason: str = Field(..., min_length=1, max_length=256)


class RefundOut(BaseModel):
    """Représentation d'un remboursement retourné par l'API.

    Attributes:
        id: Identifiant interne du remboursement.
        order_id: Identifiant de la commande liée.
        stripe_refund_id: Identifiant Stripe du remboursement (re_...).
        amount: Montant remboursé en centimes.
        reason: Motif libre fourni par le staff.
        status: État courant — ``pending``, ``succeeded`` ou ``failed``.
        created_at: Horodatage de création.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    stripe_refund_id: str
    amount: int
    reason: str | None
    status: str
    failure_reason: str | None = None
    created_by_user_id: int | None = None
    created_at: datetime


class PaymentFinalizeOut(BaseModel):
    payment: PaymentOut
    order_confirmed: bool
    refund_attempted: bool = False
    refund: RefundOut | None = None
    user_message: str | None = None
    staff_alert: dict | None = None


class PaymentDetailOut(BaseModel):
    order_id: int
    payment: PaymentOut
    paid_amount_cents: int
    refunded_amount_cents: int
    remaining_refundable_cents: int
    refunds: list[RefundOut] = Field(default_factory=list)
    receipt_url: str | None = None


class PaymentListItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    provider: str
    provider_payment_id: str | None
    provider_account_id: str | None = None
    external_reference: str | None = None
    amount: float
    amount_received: float | None = None
    currency: str
    status: str
    created_by_user_id: int | None = None
    created_at: datetime | None = None
    refunded_amount_cents: int = 0


class PaymentSummaryOut(BaseModel):
    collected_amount_cents: int
    refunded_amount_cents: int
    net_amount_cents: int
    payment_count: int
    refund_count: int
    counts_by_status: dict[str, int]


class PaymentCleanupOut(BaseModel):
    expired_count: int
    cancelled_count: int
    failed_cancellations: list[dict] = Field(default_factory=list)
    payment_ids: list[int] = Field(default_factory=list)
