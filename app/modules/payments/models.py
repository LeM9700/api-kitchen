from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("purpose IN ('sale', 'guarantee')", name="ck_payments_purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="stripe")
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    amount_received: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # 'sale' : paiement de la commande. 'guarantee' : empreinte bancaire (pre-autorisation du
    # total, capture manuelle) pour une livraison payee a la remise. Statuts d'une garantie :
    # pending -> authorized (fonds bloques) -> released (rien debite) | paid (debit total ou
    # partiel, voir captured_amount).
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="sale", server_default="sale")
    # Conditions affichees puis acceptees par le client avant l'empreinte (trace opposable).
    guarantee_terms_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    guarantee_terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Montant reellement debite quand une garantie est capturee (peut etre inferieur a `amount`).
    captured_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settlement_note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class Refund(Base):
    """Remboursement Stripe associé à un paiement et une commande.

    Attributes:
        id: Clé primaire.
        order_id: Référence vers la commande remboursée.
        payment_id: Référence vers le paiement source.
        stripe_refund_id: Identifiant Stripe du remboursement (re_...).
        amount: Montant remboursé en centimes.
        reason: Motif libre fourni par le staff (optionnel).
        status: État du remboursement — ``pending``, ``succeeded`` ou ``failed``.
        created_at: Horodatage de création (géré par la DB).
    """

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    stripe_refund_id: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[int] = mapped_column(Integer)  # en centimes
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedWebhookEvent(Base):
    __tablename__ = "processed_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
