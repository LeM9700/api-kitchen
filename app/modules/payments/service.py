import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any

import anyio
import stripe
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.loyalty.account.models import LoyaltyPointReservation
from app.modules.loyalty.account.service import confirm_checkout_reservation
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments.models import Payment, Refund
from app.modules.payments.schemas import (
    PaymentCleanupOut,
    PaymentDetailOut,
    PaymentFinalizeOut,
    PaymentListItemOut,
    PaymentOut,
    PaymentSummaryOut,
    RefundOut,
)

stripe.api_key = settings.stripe_secret_key

PAYMENT_STATUS_PAID = {"paid", "partially_refunded"}
MANUAL_PAYMENT_PROVIDERS = {"cash", "external_terminal", "cash_register"}
WEBHOOK_TOLERANCE_SECONDS = 300
EXPIRED_PAYMENT_HOURS = 24


@dataclass(frozen=True)
class StripeContext:
    account_id: str | None = None

    @property
    def options(self) -> dict[str, str]:
        return {"stripe_account": self.account_id} if self.account_id else {}


def _money_to_cents(value: Any) -> int:
    return int(round(float(value) * 100))


def _local_fallback_allowed() -> bool:
    return (settings.environment or "").lower() in {"local", "dev", "development", "test", "testing"}


def _safe_stripe_message(exc: Exception) -> str:
    user_message = getattr(exc, "user_message", None)
    message = user_message or "Stripe request failed"
    for secret in (settings.stripe_secret_key, settings.stripe_webhook_secret):
        if secret and secret in message:
            message = message.replace(secret, "[redacted]")
    return message


def _payment_out(payment: Payment, receipt_url: str | None = None) -> PaymentOut:
    return PaymentOut(
        id=payment.id,
        order_id=payment.order_id,
        provider=payment.provider,
        provider_payment_id=payment.provider_payment_id,
        external_reference=getattr(payment, "external_reference", None),
        amount=float(payment.amount),
        amount_received=(
            float(payment.amount_received)
            if getattr(payment, "amount_received", None) is not None
            else None
        ),
        currency=payment.currency,
        status=payment.status,
        created_by_user_id=getattr(payment, "created_by_user_id", None),
        receipt_url=receipt_url,
    )


def _stripe_object_to_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict_recursive", None)
    if callable(to_dict):
        return to_dict()
    return dict(value) if hasattr(value, "items") else {"value": str(value)}


def _require_customer_payment_owner(payment: Payment, order: Order, user_id: int | None, is_staff: bool) -> None:
    if user_id is None or is_staff:
        return
    if order.user_id is None or int(order.user_id) != int(user_id):
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)
    created_by_user_id = getattr(payment, "created_by_user_id", None)
    if created_by_user_id is not None and int(created_by_user_id) != int(user_id):
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)


def _validate_payment_intent_payload(
    intent: dict,
    payment: Payment,
    order: Order,
    tenant_slug: str,
) -> None:
    if str(intent.get("status") or "") != "succeeded":
        raise AppError("PAYMENT_NOT_SUCCEEDED", "Stripe payment has not succeeded.", 409)

    expected_amount = _money_to_cents(payment.amount)
    if _money_to_cents(order.total) != expected_amount:
        raise AppError("PAYMENT_ORDER_AMOUNT_MISMATCH", "Local payment amount does not match the order.", 409)

    intent_amount = intent.get("amount")
    if intent_amount is None or int(intent_amount) != expected_amount:
        raise AppError("PAYMENT_AMOUNT_MISMATCH", "Stripe payment amount does not match the order.", 409)

    amount_received = intent.get("amount_received")
    if amount_received is not None and int(amount_received) < expected_amount:
        raise AppError("PAYMENT_AMOUNT_NOT_RECEIVED", "Stripe payment amount has not been fully received.", 409)

    expected_currency = str(payment.currency or "EUR").lower()
    if str(intent.get("currency") or "").lower() != expected_currency:
        raise AppError("PAYMENT_CURRENCY_MISMATCH", "Stripe payment currency does not match the order.", 409)

    metadata = dict(intent.get("metadata") or {})
    expected_metadata = {
        "tenant_slug": tenant_slug,
        "order_id": str(order.id),
        "payment_id": str(payment.id),
    }
    for key, expected_value in expected_metadata.items():
        if str(metadata.get(key) or "") != expected_value:
            raise AppError("PAYMENT_METADATA_MISMATCH", "Stripe payment metadata does not match the order.", 409)


async def _verify_payment_intent_with_stripe(
    session: AsyncSession,
    tenant_slug: str,
    payment: Payment,
    order: Order,
) -> None:
    provider_payment_id = payment.provider_payment_id or ""
    if provider_payment_id.startswith("local_"):
        if _local_fallback_allowed():
            return
        raise AppError("PAYMENT_CONFIRMATION_REQUIRED", "Stripe payment confirmation is required.", 409)

    stripe_context = await get_stripe_context(session, tenant_slug)
    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.retrieve(
                provider_payment_id,
                **stripe_context.options,
            )
        )
    except Exception as exc:
        raise AppError("STRIPE_PAYMENT_VERIFY_FAILED", _safe_stripe_message(exc), 502) from exc

    _validate_payment_intent_payload(
        _stripe_object_to_dict(intent),
        payment,
        order,
        tenant_slug,
    )


def _is_active_pending_payment(payment: Payment, order: Order, now: datetime) -> bool:
    if payment.provider_payment_id is None:
        return False
    if payment.status != "pending" or payment.provider != "stripe":
        return False
    if _money_to_cents(payment.amount) != _money_to_cents(order.total):
        return False

    expires_at = payment.expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


async def _client_secret_for_existing_intent(payment: Payment, stripe_context: StripeContext) -> str:
    provider_payment_id = payment.provider_payment_id
    if provider_payment_id is None:
        raise AppError("PAYMENT_INTENT_NOT_READY", "Payment intent is not ready.", 409)
    if provider_payment_id.startswith("local_"):
        return provider_payment_id

    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.retrieve(
                provider_payment_id,
                **stripe_context.options,
            )
        )
    except Exception as exc:
        if not _local_fallback_allowed():
            raise AppError("STRIPE_PAYMENT_REUSE_FAILED", _safe_stripe_message(exc), 502) from exc
        return provider_payment_id

    client_secret = intent.get("client_secret") if isinstance(intent, dict) else getattr(intent, "client_secret", None)
    if not client_secret:
        raise AppError("PAYMENT_INTENT_REUSE_FAILED", "Existing PaymentIntent has no client secret.", 502)
    return str(client_secret)


async def get_stripe_context(session: AsyncSession, tenant_slug: str) -> StripeContext:
    """Resolve the Stripe Connect account for a tenant.

    The public `tenant_configs.stripe_account_id` column already exists, so the
    payments module reads it as an account reference instead of storing secrets.
    """
    row = await session.execute(
        text(
            "SELECT tc.stripe_account_id "
            "FROM public.tenant_configs tc "
            "JOIN public.tenants t ON t.id = tc.tenant_id "
            "WHERE t.slug = :slug"
        ),
        {"slug": tenant_slug},
    )
    account_id = row.scalar_one_or_none()
    if account_id:
        return StripeContext(account_id=str(account_id))
    if (settings.environment or "").lower() in {"production", "prod"}:
        raise AppError(
            "PAYMENT_PROVIDER_NOT_CONFIGURED",
            "Stripe Connect onboarding is required for this tenant.",
            409,
        )
    return StripeContext()


async def create_intent(
    session: AsyncSession,
    order_id: int,
    tenant_slug: str = "default",
    user_id: int | None = None,
) -> dict:
    order_result = await session.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    order = order_result.scalar_one_or_none()
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if user_id is not None and (order.user_id is None or int(order.user_id) != int(user_id)):
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if getattr(order, "payment_status", None) == "paid":
        raise AppError("ORDER_ALREADY_PAID", "Order is already paid.", 409)

    stripe_context = await get_stripe_context(session, tenant_slug)
    now = datetime.now(timezone.utc)
    filters = [
        Payment.order_id == order.id,
        Payment.provider == "stripe",
        Payment.status == "pending",
    ]
    if user_id is None:
        filters.append(Payment.created_by_user_id.is_(None))
    else:
        filters.append(Payment.created_by_user_id == user_id)

    existing_payment = await session.scalar(
        select(Payment)
        .where(*filters)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
    )
    if existing_payment is not None and _is_active_pending_payment(existing_payment, order, now):
        client_secret = await _client_secret_for_existing_intent(existing_payment, stripe_context)
        return {"payment": existing_payment, "client_secret": client_secret}

    payment = Payment(
        order_id=order.id,
        provider="stripe",
        amount=order.total,
        currency="EUR",
        provider_account_id=stripe_context.account_id,
        created_by_user_id=user_id,
        expires_at=now + timedelta(hours=EXPIRED_PAYMENT_HOURS),
        status="pending",
    )
    session.add(payment)
    await session.flush()

    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.create(
                amount=_money_to_cents(order.total),
                currency="eur",
                metadata={
                    "tenant_slug": tenant_slug,
                    "order_id": str(order.id),
                    "payment_id": str(payment.id),
                },
                **stripe_context.options,
            )
        )
        payment.provider_payment_id = intent["id"]
        client_secret = intent["client_secret"]
    except Exception as exc:
        if not _local_fallback_allowed():
            await session.rollback()
            raise AppError("STRIPE_PAYMENT_FAILED", _safe_stripe_message(exc), 502) from exc
        payment.provider_payment_id = f"local_{payment.id}"
        client_secret = payment.provider_payment_id

    await session.commit()
    await session.refresh(payment)
    return {"payment": payment, "client_secret": client_secret}


async def create_terminal_connection_token(
    session: AsyncSession,
    tenant_slug: str,
) -> dict:
    stripe_context = await get_stripe_context(session, tenant_slug)
    try:
        token = await anyio.to_thread.run_sync(
            lambda: stripe.terminal.ConnectionToken.create(**stripe_context.options)
        )
        return {"secret": token["secret"]}
    except Exception as exc:
        if not _local_fallback_allowed():
            raise AppError("STRIPE_TERMINAL_FAILED", _safe_stripe_message(exc), 502) from exc
        return {"secret": "local_terminal_connection_token"}


async def create_terminal_intent(
    session: AsyncSession,
    order_id: int,
    tenant_slug: str,
    user_id: int | None,
    reader_id: str | None = None,
    process_on_reader: bool = False,
) -> dict:
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if process_on_reader and not reader_id:
        raise AppError("TERMINAL_READER_REQUIRED", "reader_id is required to process on reader", 422, "reader_id")

    stripe_context = await get_stripe_context(session, tenant_slug)
    payment = Payment(
        order_id=order.id,
        provider="stripe_terminal",
        amount=order.total,
        currency="EUR",
        provider_account_id=stripe_context.account_id,
        created_by_user_id=user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=EXPIRED_PAYMENT_HOURS),
        status="pending",
    )
    session.add(payment)
    await session.flush()

    reader_action: dict | None = None
    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.create(
                amount=_money_to_cents(order.total),
                currency="eur",
                payment_method_types=["card_present"],
                capture_method="automatic",
                metadata={
                    "tenant_slug": tenant_slug,
                    "order_id": str(order.id),
                    "payment_id": str(payment.id),
                    "source": "stripe_terminal",
                },
                **stripe_context.options,
            )
        )
        payment.provider_payment_id = intent["id"]
        client_secret = intent.get("client_secret")
        if process_on_reader and reader_id:
            reader = await anyio.to_thread.run_sync(
                lambda: stripe.terminal.Reader.process_payment_intent(
                    reader_id,
                    payment_intent=intent["id"],
                    **stripe_context.options,
                )
            )
            reader_action = _stripe_object_to_dict(reader)
    except Exception as exc:
        if not _local_fallback_allowed():
            await session.rollback()
            raise AppError("STRIPE_TERMINAL_FAILED", _safe_stripe_message(exc), 502) from exc
        payment.provider_payment_id = f"local_terminal_{payment.id}"
        client_secret = payment.provider_payment_id
        if process_on_reader:
            reader_action = {
                "id": reader_id or "local_reader",
                "action": {"type": "process_payment_intent", "status": "local"},
            }

    await session.commit()
    await session.refresh(payment)
    return {
        "client_secret": client_secret,
        "payment": _payment_out(payment),
        "reader_action": reader_action,
    }


async def list_terminal_readers(
    session: AsyncSession,
    tenant_slug: str,
    location_id: str | None = None,
) -> dict:
    stripe_context = await get_stripe_context(session, tenant_slug)
    try:
        readers = await anyio.to_thread.run_sync(
            lambda: stripe.terminal.Reader.list(
                **({"location": location_id} if location_id else {}),
                **stripe_context.options,
            )
        )
        data = readers.get("data", []) if isinstance(readers, dict) else getattr(readers, "data", [])
        return {"readers": [_stripe_object_to_dict(reader) for reader in data]}
    except Exception as exc:
        if not _local_fallback_allowed():
            raise AppError("STRIPE_TERMINAL_FAILED", _safe_stripe_message(exc), 502) from exc
        return {"readers": []}


async def process_terminal_reader(
    session: AsyncSession,
    tenant_slug: str,
    reader_id: str,
    provider_payment_id: str,
) -> dict:
    payment = await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)

    stripe_context = await get_stripe_context(session, tenant_slug)
    try:
        reader = await anyio.to_thread.run_sync(
            lambda: stripe.terminal.Reader.process_payment_intent(
                reader_id,
                payment_intent=provider_payment_id,
                **stripe_context.options,
            )
        )
        return {"reader": _stripe_object_to_dict(reader)}
    except Exception as exc:
        if not _local_fallback_allowed():
            raise AppError("STRIPE_TERMINAL_FAILED", _safe_stripe_message(exc), 502) from exc
        return {
            "reader": {
                "id": reader_id,
                "action": {"type": "process_payment_intent", "status": "local"},
            }
        }


async def cancel_terminal_reader_action(
    session: AsyncSession,
    tenant_slug: str,
    reader_id: str,
) -> dict:
    stripe_context = await get_stripe_context(session, tenant_slug)
    try:
        reader = await anyio.to_thread.run_sync(
            lambda: stripe.terminal.Reader.cancel_action(reader_id, **stripe_context.options)
        )
        return {"reader": _stripe_object_to_dict(reader)}
    except Exception as exc:
        if not _local_fallback_allowed():
            raise AppError("STRIPE_TERMINAL_FAILED", _safe_stripe_message(exc), 502) from exc
        return {
            "reader": {
                "id": reader_id,
                "action": {"type": "cancel_action", "status": "local"},
            }
        }


async def confirm(
    session: AsyncSession,
    provider_payment_id: str,
    tenant_slug: str = "default",
    user_id: int | None = None,
    is_staff: bool = False,
) -> Payment:
    result = await finalize_payment(
        session,
        tenant_slug,
        provider_payment_id,
        source="confirm",
        user_id=user_id,
        is_staff=is_staff,
        verify_with_stripe=True,
    )
    return await session.get(Payment, result.payment.id)


async def confirm_local_test_payment(
    session: AsyncSession,
    order_id: int,
    tenant_slug: str,
    user_id: int | None,
    is_staff: bool = False,
) -> Payment:
    if not _local_fallback_allowed():
        raise AppError(
            "LOCAL_TEST_PAYMENT_DISABLED",
            "Local test payments are only available outside production.",
            403,
        )

    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if user_id is not None and not is_staff and (
        order.user_id is None or int(order.user_id) != int(user_id)
    ):
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if getattr(order, "payment_status", None) == "paid":
        existing = await session.scalar(
            select(Payment)
            .where(Payment.order_id == order.id, Payment.status == "paid")
            .order_by(Payment.created_at.desc(), Payment.id.desc())
        )
        if existing is not None:
            return existing
        raise AppError("ORDER_ALREADY_PAID", "Order is already paid.", 409)

    payment = Payment(
        order_id=order.id,
        provider="local_web_test",
        amount=order.total,
        currency="EUR",
        status="pending",
        created_by_user_id=user_id,
    )
    session.add(payment)
    await session.flush()
    payment.provider_payment_id = f"local_web_{payment.id}"
    await session.commit()

    result = await finalize_payment(
        session,
        tenant_slug,
        payment.provider_payment_id,
        source="local_web_test",
        user_id=user_id,
        is_staff=is_staff,
        verify_with_stripe=False,
    )
    return await session.get(Payment, result.payment.id)


async def finalize_payment(
    session: AsyncSession,
    tenant_slug: str,
    provider_payment_id: str,
    source: str = "webhook",
    user_id: int | None = None,
    is_staff: bool = False,
    verify_with_stripe: bool = False,
    stripe_payment_intent: dict | None = None,
) -> PaymentFinalizeOut:
    payment = await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)

    order = await session.get(Order, payment.order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    _require_customer_payment_owner(payment, order, user_id, is_staff)

    if payment.status in {"paid", "refunded", "partially_refunded"}:
        return PaymentFinalizeOut(
            payment=_payment_out(payment),
            order_confirmed=True,
            user_message="Payment already finalized.",
        )

    if stripe_payment_intent is not None:
        _validate_payment_intent_payload(
            _stripe_object_to_dict(stripe_payment_intent),
            payment,
            order,
            tenant_slug,
        )
    elif verify_with_stripe:
        await _verify_payment_intent_with_stripe(session, tenant_slug, payment, order)

    payment.status = "paid"
    order.payment_status = "paid"
    # Stocker user_id avant le commit (l'objet order peut être expiré après commit)
    order_user_id: int | None = order.user_id
    order_id_ref: int = order.id

    try:
        if order.status == "pending":
            await orders_service.update_status(
                session,
                order.id,
                "confirmed",
                tenant_slug=tenant_slug,
                is_staff=False,
            )
        else:
            await session.commit()
    except AppError as exc:
        await session.rollback()
        refund, alert_payload = await _auto_refund_after_confirmation_failure(
            session,
            tenant_slug,
            payment.id,
            exc,
            source,
        )
        refreshed = await session.get(Payment, payment.id)
        return PaymentFinalizeOut(
            payment=_payment_out(refreshed),
            order_confirmed=False,
            refund_attempted=True,
            refund=refund,
            user_message=(
                "Votre paiement a ete recu, mais la commande ne peut pas etre "
                "confirmee car un produit ou ingredient est indisponible. "
                "Un remboursement automatique a ete declenche."
            ),
            staff_alert=alert_payload,
        )

    # FF-03 : confirme automatiquement la réservation de points fidélité si elle existe.
    # Les erreurs sont absorbées — le paiement est déjà validé.
    await _auto_confirm_loyalty_reservation(session, order_id_ref, order_user_id)

    refreshed = await session.get(Payment, payment.id)
    return PaymentFinalizeOut(
        payment=_payment_out(refreshed),
        order_confirmed=True,
        user_message="Payment finalized and order confirmation requested.",
    )


async def _auto_refund_after_confirmation_failure(
    session: AsyncSession,
    tenant_slug: str,
    payment_id: int,
    exc: AppError,
    source: str,
) -> tuple[RefundOut | None, dict]:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)

    order = await session.get(Order, payment.order_id)
    if order is not None:
        order.payment_status = "paid"
    payment.status = "paid"
    await session.commit()

    alert_payload = {
        "tenant_slug": tenant_slug,
        "order_id": payment.order_id,
        "payment_id": payment.id,
        "source": source,
        "reason": exc.code,
        "detail": exc.detail,
        "user_message": (
            "Produit ou ingredient indisponible apres paiement. "
            "Verifier le stock, desactiver le produit si besoin, puis informer le client."
        ),
    }

    try:
        refund = await create_refund(
            session,
            tenant_slug=tenant_slug,
            order_id=payment.order_id,
            user_id=None,
            amount=None,
            reason=f"auto_refund_{exc.code.lower()}",
            allow_unfulfilled_order=True,
        )
        payment = await session.get(Payment, payment.id)
        payment.status = "refunded"
        await session.commit()
        return refund, alert_payload
    except AppError as refund_exc:
        payment = await session.get(Payment, payment.id)
        payment.status = "refund_failed"
        await session.commit()
        alert_payload["refund_error"] = refund_exc.detail
        return None, alert_payload


async def handle_webhook(session: AsyncSession, tenant_slug: str, payload: dict) -> None:
    """Dispatch les Stripe webhook events vers les handlers appropriés.

    Events traités :
    - ``payment_intent.succeeded``        → finalise le paiement et confirme la commande
    - ``payment_intent.payment_failed``   → marque le paiement en échec
    - ``payment_intent.canceled``         → marque le paiement en échec
    - ``charge.dispute.created``          → alerte SIEM (log critique)
    - ``charge.refunded``                 → synchronise le statut de remboursement

    Idempotency : chaque event Stripe (``event.id``) est enregistré dans
    ``processed_webhook_events`` (contrainte UNIQUE) avant tout traitement. Si
    l'insertion ne prend pas (event déjà vu — retry Stripe / rejeu manuel),
    l'event est ignoré sans être retraité. Protège contre les rejeux en rafale
    qui liraient le même état "pending" et double-traiteraient le paiement.

    Args:
        session: Session SQLAlchemy du tenant concerné.
        tenant_slug: Slug du tenant extrait des métadonnées de l'event.
        payload: Payload Stripe complet (event dict).
    """
    event_id: str | None = payload.get("id")
    event_type: str = payload.get("type") or ""
    data_object: dict = payload.get("data", {}).get("object", {})

    if event_id:
        result = await session.execute(
            text(
                "INSERT INTO processed_webhook_events (stripe_event_id, event_type) "
                "VALUES (:event_id, :event_type) ON CONFLICT (stripe_event_id) DO NOTHING"
            ),
            {"event_id": event_id, "event_type": event_type},
        )
        await session.commit()
        if result.rowcount == 0:
            logger.info("Webhook event %s already processed, skipping (tenant=%s)", event_id, tenant_slug)
            return

    if event_type in {"payment_intent.succeeded", ""}:
        pi_id = data_object.get("id")
        if pi_id:
            await finalize_payment(
                session,
                tenant_slug,
                pi_id,
                source="webhook",
                stripe_payment_intent=data_object,
            )

    elif event_type in {"payment_intent.payment_failed", "payment_intent.canceled"}:
        pi_id = data_object.get("id")
        if pi_id:
            await handle_payment_failure(session, pi_id)

    elif event_type == "charge.dispute.created":
        await _handle_dispute_alert(tenant_slug, data_object)

    elif event_type == "charge.refunded":
        # L'objet est un Charge — le PaymentIntent ID est dans data_object.payment_intent
        pi_id = data_object.get("payment_intent")
        if pi_id:
            await handle_charge_refunded(session, pi_id, data_object)

    else:
        logger.debug("Unhandled Stripe event type: %s (tenant=%s)", event_type, tenant_slug)


async def handle_payment_failure(session: AsyncSession, provider_payment_id: str) -> None:
    """Marque un paiement en échec suite à un event payment_intent.payment_failed ou .canceled.

    Idempotent : si le paiement est déjà payé/remboursé, ne fait rien.
    L'ordre n'est pas annulé automatiquement — le staff décide de la suite.

    Args:
        session: Session SQLAlchemy du tenant.
        provider_payment_id: Identifiant Stripe du PaymentIntent (pi_xxx).
    """
    payment = await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    if payment is None:
        logger.debug("handle_payment_failure: payment not found for %s", provider_payment_id)
        return

    if payment.status in {"paid", "partially_refunded", "refunded", "failed"}:
        return  # Déjà traité — idempotent

    payment.status = "failed"
    order = await session.get(Order, payment.order_id)
    if order is not None:
        order.payment_status = "failed"

    await session.commit()
    logger.warning(
        "Payment marked as failed: payment_id=%s order_id=%s provider=%s",
        payment.id,
        payment.order_id,
        provider_payment_id,
    )


async def handle_charge_refunded(
    session: AsyncSession,
    provider_payment_id: str,
    charge_object: dict,
) -> None:
    """Synchronise le statut de remboursement depuis un event charge.refunded Stripe.

    Utilisé quand le remboursement est initié depuis le dashboard Stripe (pas via notre API).
    Met à jour payment.status en ``partially_refunded`` ou ``refunded`` selon les montants.

    [⚠️ PROD] Ne crée pas d'enregistrement Refund en base — uniquement la mise à jour du statut
    Payment. À surveiller si les dashboards refunds doivent refléter ce cas.

    Args:
        session: Session SQLAlchemy du tenant.
        provider_payment_id: Identifiant Stripe du PaymentIntent (pi_xxx).
        charge_object: Objet ``charge`` reçu dans le webhook.
    """
    payment = await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    if payment is None:
        logger.debug("handle_charge_refunded: payment not found for PI %s", provider_payment_id)
        return

    if payment.status == "refunded":
        return  # Déjà totalement remboursé — idempotent

    amount_total_cents: int = int(charge_object.get("amount", 0))
    amount_refunded_cents: int = int(charge_object.get("amount_refunded", 0))

    if amount_refunded_cents <= 0 or amount_total_cents <= 0:
        return

    if amount_refunded_cents >= amount_total_cents:
        payment.status = "refunded"
    else:
        payment.status = "partially_refunded"

    await session.commit()
    logger.info(
        "Payment status synced from Stripe refund: payment_id=%s status=%s refunded=%d/%d",
        payment.id,
        payment.status,
        amount_refunded_cents,
        amount_total_cents,
    )


async def _handle_dispute_alert(tenant_slug: str, dispute_object: dict) -> None:
    """Émet une alerte critique lors d'un litige Stripe (charge.dispute.created).

    [⚠️ PROD] Les litiges représentent un risque financier direct. Cette alerte doit
    être traitée manuellement dans le dashboard Stripe sous 7 jours.

    TODO: intégrer au SIEM MongoDB (app.modules.admin.security) pour alerte temps réel.

    Args:
        tenant_slug: Slug du tenant concerné.
        dispute_object: Objet ``dispute`` reçu dans le webhook.
    """
    logger.critical(
        "STRIPE DISPUTE CREATED — action requise dans le dashboard Stripe sous 7 jours. "
        "tenant=%s dispute_id=%s amount=%s currency=%s reason=%s payment_intent=%s",
        tenant_slug,
        dispute_object.get("id"),
        dispute_object.get("amount"),
        dispute_object.get("currency"),
        dispute_object.get("reason"),
        dispute_object.get("payment_intent"),
    )


async def _auto_confirm_loyalty_reservation(
    session: AsyncSession,
    order_id: int,
    user_id: int | None,
) -> None:
    """Confirme automatiquement la réservation de points fidélité liée à une commande.

    Appelé par finalize_payment() après un paiement réussi. Idempotent si aucune
    réservation active n'existe. Les erreurs sont absorbées pour ne pas bloquer
    le flux de paiement déjà validé.

    Args:
        session: Session SQLAlchemy du tenant.
        order_id: Identifiant de la commande.
        user_id: Identifiant de l'utilisateur (None = commande invité, sans loyalty).
    """
    if user_id is None:
        return  # Commande invité — pas de fidélité

    now = datetime.now(timezone.utc)
    reservation = await session.scalar(
        select(LoyaltyPointReservation).where(
            LoyaltyPointReservation.order_id == order_id,
            LoyaltyPointReservation.user_id == user_id,
            LoyaltyPointReservation.status == "reserved",
            LoyaltyPointReservation.expires_at > now,
        )
    )
    if reservation is None:
        return  # Pas de réservation active — rien à faire

    try:
        await confirm_checkout_reservation(session, user_id, reservation.id)
        logger.info(
            "Loyalty reservation auto-confirmed: reservation_id=%s order_id=%s user_id=%s",
            reservation.id,
            order_id,
            user_id,
        )
    except AppError as exc:
        # [⚠️ PROD] Ne bloque pas le paiement — la réservation expirera dans 30 min.
        # Surveiller les logs LOYALTY_RESERVATION_AUTO_CONFIRM_FAILED en production.
        logger.warning(
            "LOYALTY_RESERVATION_AUTO_CONFIRM_FAILED: reservation_id=%s order_id=%s "
            "user_id=%s error=%s detail=%s",
            reservation.id,
            order_id,
            user_id,
            exc.code,
            exc.detail,
        )


async def extract_tenant_slug_from_event(event: dict) -> str:
    """Extrait le tenant_slug depuis les métadonnées d'un event Stripe.

    Pour les PaymentIntent events (payment_intent.*), les métadonnées sont dans
    ``data.object.metadata``. Pour les Charge et Dispute events (charge.*), la
    fonction récupère le PaymentIntent associé via l'API Stripe pour obtenir les
    métadonnées d'origine.

    Args:
        event: Payload Stripe complet (event dict).

    Returns:
        Le slug du tenant extrait des métadonnées.

    Raises:
        AppError: WEBHOOK_TENANT_REQUIRED si le tenant_slug est introuvable.
    """
    data = event.get("data", {}).get("object", {})
    metadata = data.get("metadata") or {}
    tenant_slug = metadata.get("tenant_slug")

    if not tenant_slug:
        # Pour charge.* et charge.dispute.*, récupérer les métadonnées depuis le PaymentIntent.
        pi_id: str | None = data.get("payment_intent")
        if pi_id and not str(pi_id).startswith("local_"):
            try:
                pi = await anyio.to_thread.run_sync(
                    lambda: stripe.PaymentIntent.retrieve(pi_id)
                )
                pi_metadata = pi.get("metadata") if isinstance(pi, dict) else getattr(pi, "metadata", None)
                tenant_slug = (pi_metadata or {}).get("tenant_slug")
            except Exception as exc:
                logger.warning("Failed to retrieve PI metadata for tenant resolution: pi=%s error=%s", pi_id, exc)

    if not tenant_slug:
        raise AppError("WEBHOOK_TENANT_REQUIRED", "Missing tenant metadata", 400, "tenant_slug")
    return str(tenant_slug)


async def _refunded_amount_cents(session: AsyncSession, payment_id: int) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id == payment_id,
            Refund.status.in_(("pending", "succeeded")),
        )
    )
    return int(total or 0)


async def _refunds_for_payment(session: AsyncSession, payment_id: int) -> list[Refund]:
    result = await session.execute(
        select(Refund).where(Refund.payment_id == payment_id).order_by(Refund.created_at.desc())
    )
    return list(result.scalars().all())


async def create_refund(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    user_id: int | None,
    amount: int | None,
    reason: str | None,
    allow_unfulfilled_order: bool = False,
) -> RefundOut:
    """Emet un remboursement Stripe total ou partiel pour une commande."""
    normalized_reason = (reason or "").strip()
    if not normalized_reason:
        raise AppError("REFUND_REASON_REQUIRED", "Refund reason is required.", 422, "reason")

    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)

    if not allow_unfulfilled_order and order.status not in {"cancelled", "delivered"}:
        raise AppError(
            "REFUND_NOT_ALLOWED",
            "Remboursement impossible : la commande doit etre annulee ou livree.",
            400,
        )

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order_id, Payment.status.in_(PAYMENT_STATUS_PAID))
    )
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Aucun paiement confirme trouve pour cette commande.", 404)

    payment_amount_cents = _money_to_cents(payment.amount)
    already_refunded = await _refunded_amount_cents(session, payment.id)
    remaining = payment_amount_cents - already_refunded
    if remaining <= 0:
        raise AppError("REFUND_ALREADY_COMPLETE", "Ce paiement est deja totalement rembourse.", 409)

    refund_amount = remaining if amount is None else amount
    if refund_amount <= 0:
        raise AppError("INVALID_REFUND_AMOUNT", "Refund amount must be positive.", 422, "amount")
    if refund_amount > remaining:
        raise AppError(
            "REFUND_AMOUNT_EXCEEDS_PAYMENT",
            "Le montant demande depasse le solde remboursable.",
            422,
            "amount",
        )

    if payment.provider in MANUAL_PAYMENT_PROVIDERS:
        now = datetime.now(timezone.utc)
        refund = Refund(
            order_id=order_id,
            payment_id=payment.id,
            stripe_refund_id=f"manual_{payment.id}_{int(now.timestamp())}",
            amount=refund_amount,
            reason=normalized_reason,
            status="succeeded",
            created_by_user_id=user_id,
            created_at=now,
        )
        session.add(refund)
        new_total = already_refunded + refund_amount
        payment.status = "refunded" if new_total >= payment_amount_cents else "partially_refunded"
        await session.commit()
        await session.refresh(refund)
        return RefundOut.model_validate(refund)

    try:
        now = datetime.now(timezone.utc)
        stripe_refund = await anyio.to_thread.run_sync(
            lambda: stripe.Refund.create(
                payment_intent=payment.provider_payment_id,
                amount=refund_amount,
                reason="requested_by_customer",
                **({"stripe_account": payment.provider_account_id} if payment.provider_account_id else {}),
            )
        )
        refund = Refund(
            order_id=order_id,
            payment_id=payment.id,
            stripe_refund_id=stripe_refund["id"],
            amount=refund_amount,
            reason=normalized_reason,
            status="succeeded",
            created_by_user_id=user_id,
            created_at=now,
        )
    except stripe.error.StripeError as exc:
        now = datetime.now(timezone.utc)
        refund = Refund(
            order_id=order_id,
            payment_id=payment.id,
            stripe_refund_id=f"failed_{payment.id}_{int(now.timestamp())}",
            amount=refund_amount,
            reason=normalized_reason,
            status="failed",
            failure_reason=_safe_stripe_message(exc),
            created_by_user_id=user_id,
            created_at=now,
        )
        session.add(refund)
        await session.commit()
        await session.refresh(refund)
        raise AppError("STRIPE_REFUND_FAILED", f"Stripe : {refund.failure_reason}", 502) from exc

    session.add(refund)
    new_total = already_refunded + refund_amount
    payment.status = "refunded" if new_total >= payment_amount_cents else "partially_refunded"
    await session.commit()
    await session.refresh(refund)
    return RefundOut.model_validate(refund)


async def get_receipt_url(payment: Payment) -> str | None:
    if not payment.provider_payment_id or payment.provider_payment_id.startswith("local_"):
        return None
    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.retrieve(
                payment.provider_payment_id,
                expand=["latest_charge"],
                **({"stripe_account": payment.provider_account_id} if payment.provider_account_id else {}),
            )
        )
    except stripe.error.StripeError:
        return None

    charge = intent.get("latest_charge") if isinstance(intent, dict) else getattr(intent, "latest_charge", None)
    if isinstance(charge, dict):
        return charge.get("receipt_url")
    return getattr(charge, "receipt_url", None)


async def get_payment_for_order(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    is_staff: bool,
    include_receipt: bool = True,
) -> PaymentDetailOut:
    order = await session.get(Order, order_id)
    if order is None or (not is_staff and order.user_id != user_id):
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc())
    )
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)

    refunds = await _refunds_for_payment(session, payment.id)
    refunded_amount = sum(r.amount for r in refunds if r.status in {"pending", "succeeded"})
    paid_amount = _money_to_cents(payment.amount) if payment.status in PAYMENT_STATUS_PAID | {"refunded"} else 0
    receipt_url = await get_receipt_url(payment) if include_receipt else None
    refund_out = [RefundOut.model_validate(r) for r in refunds]
    return PaymentDetailOut(
        order_id=order_id,
        payment=_payment_out(payment, receipt_url),
        paid_amount_cents=paid_amount,
        refunded_amount_cents=refunded_amount,
        remaining_refundable_cents=max(0, _money_to_cents(payment.amount) - refunded_amount),
        refunds=refund_out,
        receipt_url=receipt_url,
    )


async def list_refunds_for_order(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    is_staff: bool,
) -> list[RefundOut]:
    detail = await get_payment_for_order(session, order_id, user_id, is_staff, include_receipt=False)
    return detail.refunds


async def list_payments(
    session: AsyncSession,
    pagination: PaginationParams,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    order_id: int | None = None,
    provider: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> tuple[list[PaymentListItemOut], int]:
    def apply_filters(query):
        if status:
            statuses = [part.strip() for part in status.split(",") if part.strip()]
            query = query.where(Payment.status.in_(statuses))
        if date_from:
            query = query.where(Payment.created_at >= date_from)
        if date_to:
            query = query.where(Payment.created_at <= date_to)
        if order_id is not None:
            query = query.where(Payment.order_id == order_id)
        if provider is not None:
            query = query.where(Payment.provider == provider)
        if min_amount is not None:
            query = query.where(Payment.amount >= min_amount)
        if max_amount is not None:
            query = query.where(Payment.amount <= max_amount)
        return query

    stmt = apply_filters(select(Payment))
    count_stmt = apply_filters(select(func.count(Payment.id)))

    total = await session.scalar(count_stmt) or 0
    result = await session.execute(
        stmt.order_by(Payment.created_at.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    payments = list(result.scalars().all())
    items: list[PaymentListItemOut] = []
    for payment in payments:
        items.append(
            PaymentListItemOut(
                id=payment.id,
                order_id=payment.order_id,
                provider=payment.provider,
                provider_payment_id=payment.provider_payment_id,
                provider_account_id=payment.provider_account_id,
                external_reference=getattr(payment, "external_reference", None),
                amount=float(payment.amount),
                amount_received=(
                    float(payment.amount_received)
                    if getattr(payment, "amount_received", None) is not None
                    else None
                ),
                currency=payment.currency,
                status=payment.status,
                created_by_user_id=getattr(payment, "created_by_user_id", None),
                created_at=payment.created_at,
                refunded_amount_cents=await _refunded_amount_cents(session, payment.id),
            )
        )
    return items, int(total)


async def export_payments_csv(
    session: AsyncSession,
    status: str | None = None,
    provider: str | None = None,
    payment_status: str | None = None,
    order_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> str:
    filters = []
    if status:
        filters.append(Payment.status.in_([part.strip() for part in status.split(",") if part.strip()]))
    if provider:
        filters.append(Payment.provider == provider)
    if payment_status:
        filters.append(
            Order.payment_status.in_([part.strip() for part in payment_status.split(",") if part.strip()])
        )
    if order_type:
        filters.append(Order.order_type == order_type)
    if date_from:
        filters.append(Payment.created_at >= date_from)
    if date_to:
        filters.append(Payment.created_at <= date_to)

    stmt = select(Payment, Order).join(Order, Payment.order_id == Order.id)
    if filters:
        stmt = stmt.where(*filters)
    result = await session.execute(stmt.order_by(Payment.created_at.desc(), Payment.id.desc()))

    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "order_id",
            "created_at",
            "provider",
            "provider_payment_id",
            "external_reference",
            "amount",
            "amount_received",
            "currency",
            "status",
            "created_by_user_id",
            "order_type",
            "order_status",
            "order_payment_status",
        ],
    )
    writer.writeheader()
    for payment, order in result.all():
        writer.writerow(
            {
                "id": payment.id,
                "order_id": payment.order_id,
                "created_at": payment.created_at,
                "provider": payment.provider,
                "provider_payment_id": payment.provider_payment_id,
                "external_reference": getattr(payment, "external_reference", None),
                "amount": float(payment.amount),
                "amount_received": (
                    float(payment.amount_received)
                    if getattr(payment, "amount_received", None) is not None
                    else None
                ),
                "currency": payment.currency,
                "status": payment.status,
                "created_by_user_id": getattr(payment, "created_by_user_id", None),
                "order_type": getattr(order, "order_type", None),
                "order_status": order.status,
                "order_payment_status": getattr(order, "payment_status", None),
            }
        )
    return output.getvalue()


async def get_payment_summary(
    session: AsyncSession,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> PaymentSummaryOut:
    payment_stmt = select(Payment)
    refund_stmt = select(Refund)
    if date_from:
        payment_stmt = payment_stmt.where(Payment.created_at >= date_from)
        refund_stmt = refund_stmt.where(Refund.created_at >= date_from)
    if date_to:
        payment_stmt = payment_stmt.where(Payment.created_at <= date_to)
        refund_stmt = refund_stmt.where(Refund.created_at <= date_to)

    payments = list((await session.execute(payment_stmt)).scalars().all())
    refunds = list((await session.execute(refund_stmt)).scalars().all())
    collected = sum(_money_to_cents(p.amount) for p in payments if p.status in PAYMENT_STATUS_PAID | {"refunded"})
    refunded = sum(r.amount for r in refunds if r.status == "succeeded")
    counts: dict[str, int] = {}
    for payment in payments:
        counts[payment.status] = counts.get(payment.status, 0) + 1
    return PaymentSummaryOut(
        collected_amount_cents=collected,
        refunded_amount_cents=refunded,
        net_amount_cents=collected - refunded,
        payment_count=len(payments),
        refund_count=len([r for r in refunds if r.status == "succeeded"]),
        counts_by_status=counts,
    )


async def cleanup_expired_pending_payments(
    session: AsyncSession,
    tenant_slug: str,
    older_than_hours: int = EXPIRED_PAYMENT_HOURS,
) -> PaymentCleanupOut:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    result = await session.execute(
        select(Payment).where(
            Payment.status == "pending",
            Payment.created_at <= cutoff,
        )
    )
    payments = list(result.scalars().all())
    cancelled_count = 0
    failures: list[dict] = []
    for payment in payments:
        if payment.provider_payment_id and not payment.provider_payment_id.startswith("local_"):
            try:
                await anyio.to_thread.run_sync(
                    lambda: stripe.PaymentIntent.cancel(
                        payment.provider_payment_id,
                        **({"stripe_account": payment.provider_account_id} if payment.provider_account_id else {}),
                    )
                )
                cancelled_count += 1
            except stripe.error.StripeError as exc:
                failures.append({"payment_id": payment.id, "detail": _safe_stripe_message(exc)})
        payment.status = "expired"
    await session.commit()
    return PaymentCleanupOut(
        expired_count=len(payments),
        cancelled_count=cancelled_count,
        failed_cancellations=failures,
        payment_ids=[p.id for p in payments],
    )
