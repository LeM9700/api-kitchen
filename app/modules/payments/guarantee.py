"""Garantie de paiement pour les livraisons payees a la remise (empreinte bancaire).

Principe : le total de la commande est **pre-autorise** sur la carte du client (PaymentIntent a
capture manuelle). Aucun debit n'a lieu :

- livraison reussie, client paie en especes -> l'empreinte est liberee, l'encaissement est
  enregistre comme un paiement ``cash`` ;
- livraison reussie, carte debitee -> l'empreinte est capturee ;
- echec de livraison de la faute du client, ou annulation -> l'admin capture tout ou partie du
  montant, ou libere l'empreinte.

Une pre-autorisation expire cote banque au bout de ~7 jours : un blocage oublie se libere seul,
et le webhook ``payment_intent.canceled`` le repercute ici.

Ce module depend de ``payments.service`` ; ``service`` ne l'importe que dans des fonctions (pas au
chargement) pour eviter l'import circulaire.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import anyio
import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments import service as pay
from app.modules.payments.models import Payment
from app.modules.payments.schemas import PaymentFinalizeOut

logger = logging.getLogger(__name__)

# Change des que le texte des conditions affichees au client change de sens : l'app envoie la
# version qu'elle a affichee, le serveur refuse une version perimee (409) et la conserve avec
# l'horodatage d'acceptation.
GUARANTEE_TERMS_VERSION = "2026-10-02"
GUARANTEE_HOLD_VALIDITY_DAYS = 7
PAYMENT_LINK_VALIDITY_MINUTES = 60

ORDER_PS_GUARANTEED = "guaranteed"
ORDER_PS_RELEASED = "guarantee_released"
ORDER_PS_CAPTURED_PARTIAL = "guarantee_captured"

# Statuts d'une garantie apres lesquels plus rien ne doit etre (re)finalise.
_SETTLED_STATUSES = {"authorized", "paid", "released", "refunded", "partially_refunded"}


def terms_policy() -> dict:
    """Politique de garantie, pour que les apps affichent les memes conditions que celles
    appliquees par le serveur."""
    return {
        "version": GUARANTEE_TERMS_VERSION,
        "hold_amount": "order_total",
        "charged_on": ["customer_absent", "wrong_address", "customer_unreachable", "order_refused"],
        "released_otherwise": True,
        "hold_validity_days": GUARANTEE_HOLD_VALIDITY_DAYS,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _intent_value(intent: dict, key: str, default=None):
    value = intent.get(key, default)
    return default if value is None else value


# --------------------------------------------------------------------------- validation Stripe


def _validate_guarantee_intent(intent: dict, payment: Payment, order: Order, tenant_slug: str) -> None:
    """Verifie qu'un PaymentIntent est bien l'empreinte attendue pour cette commande."""
    if str(intent.get("status") or "") != "requires_capture":
        raise AppError("GUARANTEE_NOT_AUTHORIZED", "La carte n'a pas ete pre-autorisee.", 409)
    if str(intent.get("capture_method") or "") != "manual":
        raise AppError("GUARANTEE_CAPTURE_METHOD_INVALID", "L'empreinte doit etre a capture manuelle.", 409)

    expected = pay._money_to_cents(payment.amount)
    if pay._money_to_cents(order.total) != expected:
        raise AppError("PAYMENT_ORDER_AMOUNT_MISMATCH", "Local payment amount does not match the order.", 409)
    if intent.get("amount") is None or int(intent["amount"]) != expected:
        raise AppError("PAYMENT_AMOUNT_MISMATCH", "Stripe payment amount does not match the order.", 409)
    capturable = intent.get("amount_capturable")
    if capturable is not None and int(capturable) < expected:
        raise AppError("GUARANTEE_AMOUNT_NOT_HELD", "Le montant total n'a pas ete bloque sur la carte.", 409)
    if str(intent.get("currency") or "").lower() != str(payment.currency or "EUR").lower():
        raise AppError("PAYMENT_CURRENCY_MISMATCH", "Stripe payment currency does not match the order.", 409)

    metadata = dict(intent.get("metadata") or {})
    expected_metadata = {
        "tenant_slug": tenant_slug,
        "order_id": str(order.id),
        "payment_id": str(payment.id),
        "purpose": "guarantee",
    }
    for key, value in expected_metadata.items():
        if str(metadata.get(key) or "") != value:
            raise AppError("PAYMENT_METADATA_MISMATCH", "Stripe payment metadata does not match the order.", 409)


async def _retrieve_intent(payment: Payment, tenant_slug: str, session: AsyncSession) -> dict:
    context = await pay.get_stripe_context(session, tenant_slug)
    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.retrieve(payment.provider_payment_id, **context.options)
        )
    except Exception as exc:
        raise AppError("STRIPE_PAYMENT_VERIFY_FAILED", pay._safe_stripe_message(exc), 502) from exc
    return pay._stripe_object_to_dict(intent)


def _is_local(payment: Payment) -> bool:
    return (payment.provider_payment_id or "").startswith("local_")


def _stripe_options(payment: Payment) -> dict:
    return {"stripe_account": payment.provider_account_id} if payment.provider_account_id else {}


# --------------------------------------------------------------------------- creation de l'empreinte


async def create_guarantee_intent(
    session: AsyncSession,
    order_id: int,
    tenant_slug: str,
    user_id: int,
    *,
    terms_version: str,
    accept_terms: bool,
) -> dict:
    """PaymentIntent a capture manuelle (pre-autorisation du total) pour une livraison payee a la
    remise. Le client doit avoir accepte les conditions affichees (version tracee)."""
    if not accept_terms:
        raise AppError(
            "GUARANTEE_TERMS_NOT_ACCEPTED",
            "Vous devez accepter les conditions de l'empreinte bancaire.",
            422,
            "accept_terms",
        )
    if terms_version != GUARANTEE_TERMS_VERSION:
        raise AppError(
            "GUARANTEE_TERMS_OUTDATED",
            "Les conditions ont change : mettez l'application a jour puis reessayez.",
            409,
            "terms_version",
        )

    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None or order.user_id is None or int(order.user_id) != int(user_id):
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.order_type != "delivery":
        raise AppError("GUARANTEE_DELIVERY_ONLY", "L'empreinte ne concerne que les livraisons.", 422)
    if order.status != "pending" or order.payment_status in {"paid", ORDER_PS_GUARANTEED}:
        raise AppError("ORDER_ALREADY_PAID", "Order is already paid or guaranteed.", 409)

    context = await pay.get_stripe_context(session, tenant_slug)
    now = _now()
    existing = await session.scalar(
        select(Payment)
        .where(
            Payment.order_id == order.id,
            Payment.provider == "stripe",
            Payment.purpose == "guarantee",
            Payment.status == "pending",
            Payment.created_by_user_id == user_id,
        )
        .order_by(Payment.created_at.desc(), Payment.id.desc())
    )
    if existing is not None and pay._is_active_pending_payment(existing, order, now):
        secret = await pay._client_secret_for_existing_intent(existing, context)
        return {"payment": existing, "client_secret": secret}

    currency = await pay._tenant_currency(session)
    payment = Payment(
        order_id=order.id,
        provider="stripe",
        purpose="guarantee",
        amount=order.total,
        currency=currency,
        provider_account_id=context.account_id,
        created_by_user_id=user_id,
        expires_at=now + timedelta(hours=pay.EXPIRED_PAYMENT_HOURS),
        status="pending",
        guarantee_terms_version=terms_version,
        guarantee_terms_accepted_at=now,
    )
    session.add(payment)
    await session.flush()

    metadata = {
        "tenant_slug": tenant_slug,
        "order_id": str(order.id),
        "payment_id": str(payment.id),
        "purpose": "guarantee",
    }
    try:
        intent = await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.create(
                amount=pay._money_to_cents(order.total),
                currency=currency.lower(),
                capture_method="manual",
                metadata=metadata,
                **context.options,
            )
        )
        payment.provider_payment_id = intent["id"]
        client_secret = intent["client_secret"]
    except Exception as exc:
        if not pay._local_fallback_allowed():
            await session.rollback()
            raise AppError("STRIPE_PAYMENT_FAILED", pay._safe_stripe_message(exc), 502) from exc
        payment.provider_payment_id = f"local_{payment.id}"
        client_secret = payment.provider_payment_id

    await session.commit()
    await session.refresh(payment)
    return {"payment": payment, "client_secret": client_secret}


# --------------------------------------------------------------------------- finalisation


async def find_payment_for_intent(session: AsyncSession, intent: dict) -> Payment | None:
    """Retrouve le paiement local d'un PaymentIntent. Pour un lien de paiement (Checkout), le
    PaymentIntent n'existe qu'une fois la page validee : le paiement local porte alors l'id de
    session ``cs_...``. On le retrouve par les metadonnees (payment_id + order_id) et on lui
    rattache l'id du PaymentIntent, quel que soit l'ordre d'arrivee des evenements Stripe."""
    intent_id = intent.get("id")
    if intent_id:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == intent_id))
        if payment is not None:
            return payment
    metadata = dict(intent.get("metadata") or {})
    try:
        payment_id = int(metadata.get("payment_id") or 0)
        order_id = int(metadata.get("order_id") or 0)
    except (TypeError, ValueError):
        return None
    if not payment_id:
        return None
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.order_id != order_id:
        return None
    if intent_id and (payment.provider_payment_id or "").startswith("cs_"):
        payment.provider_payment_id = intent_id
        await session.flush()
    return payment


async def finalize_guarantee(
    session: AsyncSession,
    tenant_slug: str,
    payment: Payment,
    order: Order,
    *,
    source: str,
    user_id: int | None = None,
    is_staff: bool = False,
    verify_with_stripe: bool = False,
    stripe_payment_intent: dict | None = None,
) -> PaymentFinalizeOut:
    """Une empreinte est (peut-etre) posee : verifie, marque la commande « garantie » et la
    confirme. Idempotent, partage par le webhook et par ``POST /payments/confirm``."""
    pay._require_customer_payment_owner(payment, order, user_id, is_staff)
    # Webhook et /confirm peuvent arriver en meme temps : on serialise sur la ligne du paiement.
    await session.refresh(payment, with_for_update=True)

    if payment.status in _SETTLED_STATUSES:
        return PaymentFinalizeOut(
            payment=pay._payment_out(payment),
            order_confirmed=True,
            user_message="Guarantee already finalized.",
        )
    if order.order_type != "delivery":
        raise AppError("GUARANTEE_DELIVERY_ONLY", "L'empreinte ne concerne que les livraisons.", 422)

    intent: dict | None = stripe_payment_intent
    if intent is None and verify_with_stripe:
        if _is_local(payment):
            if not pay._local_fallback_allowed():
                raise AppError("PAYMENT_CONFIRMATION_REQUIRED", "Stripe payment confirmation is required.", 409)
        else:
            intent = await _retrieve_intent(payment, tenant_slug, session)

    if intent is not None:
        if str(intent.get("status") or "") == "succeeded":
            # Capturee en dehors de l'API (tableau de bord Stripe) : on synchronise.
            return await _sync_external_capture(session, payment, order, intent)
        _validate_guarantee_intent(intent, payment, order, tenant_slug)

    if payment.guarantee_terms_version is None:
        # Lien de paiement : le client a valide la page Stripe qui affichait les conditions.
        payment.guarantee_terms_version = GUARANTEE_TERMS_VERSION
        payment.guarantee_terms_accepted_at = _now()

    payment.status = "authorized"
    order.payment_status = ORDER_PS_GUARANTEED
    order_id_ref = order.id
    order_user_id = order.user_id
    payment_id_ref = payment.id

    try:
        if order.status == "pending":
            await orders_service.update_status(
                session, order.id, "confirmed", tenant_slug=tenant_slug, is_staff=False
            )
        else:
            await session.commit()
    except AppError as exc:
        await session.rollback()
        # Stock insuffisant : on ne garde pas une empreinte sur une commande qui ne partira pas.
        # Le rollback a remis le paiement en « pending » : on libere directement chez Stripe.
        payment = await session.get(Payment, payment_id_ref)
        order = await session.get(Order, order_id_ref)
        try:
            await _cancel_intent_idempotent(payment)
            payment.status = "released"
            payment.settled_at = _now()
            payment.settlement_note = f"auto_release_{exc.code.lower()}"[:256]
            order.payment_status = ORDER_PS_RELEASED
        except AppError:
            # Annulation Stripe impossible : on garde l'empreinte tracee, le staff est alerte.
            payment.status = "authorized"
            order.payment_status = ORDER_PS_GUARANTEED
            logger.error("guarantee auto-release failed order_id=%s", order_id_ref)
        await session.commit()
        refreshed = await session.get(Payment, payment_id_ref)
        return PaymentFinalizeOut(
            payment=pay._payment_out(refreshed),
            order_confirmed=False,
            user_message=(
                "Votre carte a ete pre-autorisee, mais la commande n'a pas pu etre confirmee "
                "(produit ou ingredient indisponible). Le blocage a ete annule, rien n'est debite."
            ),
            staff_alert={
                "tenant_slug": tenant_slug,
                "order_id": order_id_ref,
                "payment_id": payment.id,
                "source": source,
                "reason": exc.code,
                "detail": exc.detail,
            },
        )

    # Reservation de points fidelite : confirmee des que la commande l'est (erreurs absorbees).
    await pay._auto_confirm_loyalty_reservation(session, order_id_ref, order_user_id)

    refreshed = await session.get(Payment, payment_id_ref)
    return PaymentFinalizeOut(
        payment=pay._payment_out(refreshed),
        order_confirmed=True,
        user_message="Carte pre-autorisee : la commande est confirmee, vous paierez a la livraison.",
    )


async def _sync_external_capture(session: AsyncSession, payment: Payment, order: Order, intent: dict) -> PaymentFinalizeOut:
    received = int(intent.get("amount_received") or intent.get("amount") or 0)
    payment.status = "paid"
    payment.captured_amount = received / 100
    payment.settled_at = _now()
    payment.settlement_note = "captured_outside_api"
    full = received >= pay._money_to_cents(order.total)
    order.payment_status = "paid" if full else ORDER_PS_CAPTURED_PARTIAL
    await session.commit()
    return PaymentFinalizeOut(
        payment=pay._payment_out(payment),
        order_confirmed=True,
        user_message="Guarantee captured.",
    )


# --------------------------------------------------------------------------- reglement


async def _authorized_guarantee(
    session: AsyncSession, order_id: int, *, payment_id: int | None = None
) -> Payment | None:
    stmt = select(Payment).where(
        Payment.order_id == order_id,
        Payment.purpose == "guarantee",
        Payment.status == "authorized",
    )
    if payment_id is not None:
        stmt = stmt.where(Payment.id == payment_id)
    return await session.scalar(stmt.order_by(Payment.id.desc()).limit(1).with_for_update())


async def _cancel_intent_idempotent(payment: Payment) -> None:
    """Annule la pre-autorisation chez Stripe. Idempotent : un blocage deja annule est un succes."""
    if _is_local(payment) or not payment.provider_payment_id:
        return
    options = _stripe_options(payment)
    try:
        await anyio.to_thread.run_sync(
            lambda: stripe.PaymentIntent.cancel(payment.provider_payment_id, **options)
        )
    except stripe.error.StripeError as exc:
        try:
            intent = await anyio.to_thread.run_sync(
                lambda: stripe.PaymentIntent.retrieve(payment.provider_payment_id, **options)
            )
        except stripe.error.StripeError:
            raise AppError("STRIPE_RELEASE_FAILED", pay._safe_stripe_message(exc), 502) from exc
        status = intent.get("status") if isinstance(intent, dict) else getattr(intent, "status", None)
        if status != "canceled":
            raise AppError("STRIPE_RELEASE_FAILED", pay._safe_stripe_message(exc), 502) from exc


async def release_guarantee(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    *,
    reason: str,
    user_id: int | None = None,
    best_effort: bool = False,
    order_payment_status: str = ORDER_PS_RELEASED,
    force_authorized_payment_id: int | None = None,
) -> Payment | None:
    """Libere l'empreinte : rien n'est debite. ``best_effort`` ne leve jamais (annulation de
    commande : on ne bloque pas le flux metier, on journalise)."""
    try:
        payment = await _authorized_guarantee(session, order_id, payment_id=force_authorized_payment_id)
        if payment is None:
            if best_effort:
                return None
            raise AppError("GUARANTEE_NOT_FOUND", "Aucune empreinte active pour cette commande.", 409)
        await _cancel_intent_idempotent(payment)
        payment.status = "released"
        payment.settled_at = _now()
        payment.settled_by_user_id = user_id
        payment.settlement_note = reason[:256]
        order = await session.get(Order, order_id)
        if order is not None:
            order.payment_status = order_payment_status
        await session.commit()
        return payment
    except AppError:
        await session.rollback()
        if best_effort:
            logger.error("guarantee release failed (best effort) order_id=%s reason=%s", order_id, reason)
            return None
        raise
    except Exception:
        await session.rollback()
        if best_effort:
            logger.exception("guarantee release crashed (best effort) order_id=%s", order_id)
            return None
        raise


async def capture_guarantee(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    *,
    amount_cents: int | None,
    reason: str,
    user_id: int | None,
) -> Payment:
    """Debite tout ou partie de l'empreinte (frais d'echec, livraison payee par carte). Le reste
    du blocage est libere automatiquement par Stripe lors d'une capture partielle."""
    reason = (reason or "").strip()
    if not reason:
        raise AppError("CAPTURE_REASON_REQUIRED", "Un motif est requis pour debiter l'empreinte.", 422, "reason")
    payment = await _authorized_guarantee(session, order_id)
    if payment is None:
        raise AppError("GUARANTEE_NOT_FOUND", "Aucune empreinte active pour cette commande.", 409)

    total_cents = pay._money_to_cents(payment.amount)
    cents = total_cents if amount_cents is None else amount_cents
    if cents <= 0 or cents > total_cents:
        raise AppError(
            "INVALID_CAPTURE_AMOUNT",
            "Le montant doit etre positif et ne pas depasser l'empreinte.",
            422,
            "amount",
        )

    if not _is_local(payment):
        options = _stripe_options(payment)
        try:
            await anyio.to_thread.run_sync(
                lambda: stripe.PaymentIntent.capture(
                    payment.provider_payment_id, amount_to_capture=cents, **options
                )
            )
        except stripe.error.StripeError as exc:
            await session.rollback()
            raise AppError("STRIPE_CAPTURE_FAILED", pay._safe_stripe_message(exc), 502) from exc

    payment.status = "paid"
    payment.captured_amount = cents / 100
    payment.settled_at = _now()
    payment.settled_by_user_id = user_id
    payment.settlement_note = reason[:256]
    order = await session.get(Order, order_id)
    if order is not None:
        order.payment_status = "paid" if cents == total_cents else ORDER_PS_CAPTURED_PARTIAL
    await session.commit()
    await session.refresh(payment)
    return payment


async def settle_guarantee_cash(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    *,
    amount_received: float | None,
    user_id: int | None,
) -> Payment:
    """Le client a paye en especes a la remise : l'empreinte est liberee et l'encaissement est
    enregistre comme un paiement ``cash`` (comptabilite et remboursements habituels)."""
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if amount_received is not None and amount_received < float(order.total):
        raise AppError(
            "CASH_AMOUNT_INSUFFICIENT",
            "Le montant recu est inferieur au total de la commande.",
            422,
            "amount_received",
        )
    guarantee = await _authorized_guarantee(session, order_id)
    if guarantee is None:
        raise AppError("GUARANTEE_NOT_FOUND", "Aucune empreinte active pour cette commande.", 409)

    await _cancel_intent_idempotent(guarantee)
    now = _now()
    guarantee.status = "released"
    guarantee.settled_at = now
    guarantee.settled_by_user_id = user_id
    guarantee.settlement_note = "cash_collected"
    cash = Payment(
        order_id=order.id,
        provider="cash",
        purpose="sale",
        amount=order.total,
        amount_received=amount_received,
        currency=guarantee.currency,
        status="paid",
        created_by_user_id=user_id,
        settled_at=now,
        settled_by_user_id=user_id,
        settlement_note="cash_on_delivery",
    )
    session.add(cash)
    order.payment_status = "paid"
    await session.commit()
    await session.refresh(cash)
    return cash


# --------------------------------------------------------------------------- lien de paiement


def _link_pages() -> tuple[str, str]:
    base = (settings.app_base_url or "").rstrip("/")
    return (
        f"{base}/api/v1/payments/public/link-done?status=ok",
        f"{base}/api/v1/payments/public/link-done?status=cancelled",
    )


def _link_sms_body(order_id: int, mode: str, url: str) -> str:
    what = "securisez votre commande (aucun debit)" if mode == "guarantee" else "payez votre commande"
    return f"Commande #{order_id} : {what} ici : {url}"


async def create_payment_link(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    *,
    mode: str,
    actor_user_id: int | None,
    arq_pool=None,
) -> dict:
    """Lien de paiement Stripe Checkout pour une commande saisie au comptoir (telephone).

    ``mode='full'`` : paiement en ligne immediat. ``mode='guarantee'`` : empreinte du total, a
    regler a la livraison (livraison uniquement). Le lien est envoye par SMS si le numero est
    exploitable, et toujours renvoye pour que le staff puisse le transmettre autrement.
    """
    if mode not in {"full", "guarantee"}:
        raise AppError("INVALID_LINK_MODE", "Mode de lien invalide.", 422, "mode")
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    if order.order_type not in {"delivery", "pickup"}:
        raise AppError("PAYMENT_LINK_UNSUPPORTED", "Lien de paiement indisponible pour ce type de commande.", 422)
    if mode == "guarantee" and order.order_type != "delivery":
        raise AppError("GUARANTEE_DELIVERY_ONLY", "L'empreinte ne concerne que les livraisons.", 422)
    if order.status != "pending" or order.payment_status in {"paid", ORDER_PS_GUARANTEED}:
        raise AppError("ORDER_ALREADY_PAID", "Order is already paid or guaranteed.", 409)

    context = await pay.get_stripe_context(session, tenant_slug)
    currency = await pay._tenant_currency(session)
    now = _now()

    # Un seul lien actif par commande : les anciens sont invalides.
    previous = list(
        (
            await session.execute(
                select(Payment).where(
                    Payment.order_id == order.id,
                    Payment.status == "pending",
                    Payment.provider == "stripe",
                )
            )
        ).scalars()
    )
    for old in previous:
        await _expire_checkout_session(old)
        old.status = "expired"

    expires_at = now + timedelta(minutes=PAYMENT_LINK_VALIDITY_MINUTES)
    payment = Payment(
        order_id=order.id,
        provider="stripe",
        purpose="guarantee" if mode == "guarantee" else "sale",
        amount=order.total,
        currency=currency,
        provider_account_id=context.account_id,
        created_by_user_id=actor_user_id,
        expires_at=expires_at,
        status="pending",
    )
    session.add(payment)
    await session.flush()
    await session.commit()  # le paiement existe meme si Stripe est injoignable (relance possible)

    metadata = {
        "tenant_slug": tenant_slug,
        "order_id": str(order.id),
        "payment_id": str(payment.id),
        "purpose": payment.purpose,
    }
    success_url, cancel_url = _link_pages()
    payment_intent_data: dict = {"metadata": metadata}
    if mode == "guarantee":
        payment_intent_data["capture_method"] = "manual"

    try:
        checkout = await anyio.to_thread.run_sync(
            lambda: stripe.checkout.Session.create(
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency.lower(),
                            "unit_amount": pay._money_to_cents(order.total),
                            "product_data": {"name": f"Commande #{order.id}"},
                        },
                        "quantity": 1,
                    }
                ],
                payment_intent_data=payment_intent_data,
                metadata=metadata,
                success_url=success_url,
                cancel_url=cancel_url,
                expires_at=int(expires_at.timestamp()),
                locale="fr",
                **({"customer_email": order.customer_email} if order.customer_email else {}),
                **(
                    {
                        "custom_text": {
                            "submit": {
                                "message": (
                                    "Votre carte est pre-autorisee du montant de la commande, sans debit. "
                                    "Vous reglez a la livraison. Le restaurant ne peut debiter la carte "
                                    "qu'en cas d'echec de livraison de votre fait ; sinon la somme est liberee."
                                )
                            }
                        }
                    }
                    if mode == "guarantee"
                    else {}
                ),
                **context.options,
            )
        )
        payment.provider_payment_id = checkout["id"]
        url = checkout["url"]
    except Exception as exc:
        if not pay._local_fallback_allowed():
            await session.commit()  # conserve le paiement en attente, sans identifiant Stripe
            raise AppError("STRIPE_PAYMENT_FAILED", pay._safe_stripe_message(exc), 502) from exc
        payment.provider_payment_id = f"cs_local_{payment.id}"
        url = f"{(settings.app_base_url or '').rstrip('/')}/api/v1/payments/public/link-done?status=local"

    await session.commit()
    await session.refresh(payment)

    delivered_via = await _send_link(order, mode, url, arq_pool)
    return {
        "url": url,
        "expires_at": expires_at,
        "mode": mode,
        "payment_id": payment.id,
        "sent_by_sms": delivered_via["sms"],
        "sent_by_email": delivered_via["email"],
    }


async def _expire_checkout_session(payment: Payment) -> None:
    session_id = payment.provider_payment_id or ""
    if not session_id.startswith("cs_") or session_id.startswith("cs_local_"):
        return
    options = _stripe_options(payment)
    try:
        await anyio.to_thread.run_sync(lambda: stripe.checkout.Session.expire(session_id, **options))
    except stripe.error.StripeError as exc:
        logger.warning("checkout session expire failed id=%s: %s", session_id, pay._safe_stripe_message(exc))


async def _send_link(order: Order, mode: str, url: str, arq_pool) -> dict:
    sent = {"sms": False, "email": False}
    if order.customer_phone:
        try:
            from app.core.sms.service import enqueue_sms
            from app.modules.customer.service import normalize_phone_e164

            await enqueue_sms(
                arq_pool,
                to_phone_e164=normalize_phone_e164(order.customer_phone),
                body=_link_sms_body(order.id, mode, url),
            )
            sent["sms"] = True
        except Exception:
            logger.warning("payment link SMS not sent for order_id=%s", order.id)
    if order.customer_email:
        try:
            from app.core.email.resend_service import send_email

            sent["email"] = await send_email(
                order.customer_email,
                f"Commande #{order.id} : paiement",
                f'<p>Pour valider votre commande #{order.id}, ouvrez ce lien securise : '
                f'<a href="{url}">{url}</a></p>',
            )
        except Exception:
            logger.warning("payment link email not sent for order_id=%s", order.id)
    return sent


async def get_payment_link(session: AsyncSession, tenant_slug: str, order_id: int) -> dict | None:
    """Lien encore ouvert d'une commande (rejeu idempotent de la creation), ou ``None``."""
    payment = await session.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.status == "pending", Payment.provider == "stripe")
        .order_by(Payment.id.desc())
        .limit(1)
    )
    if payment is None or not (payment.provider_payment_id or "").startswith("cs_"):
        return None
    if payment.provider_payment_id.startswith("cs_local_"):
        return {
            "url": f"{(settings.app_base_url or '').rstrip('/')}/api/v1/payments/public/link-done?status=local",
            "expires_at": payment.expires_at,
            "mode": "guarantee" if payment.purpose == "guarantee" else "full",
            "payment_id": payment.id,
            "sent_by_sms": False,
            "sent_by_email": False,
        }
    options = _stripe_options(payment)
    try:
        checkout = await anyio.to_thread.run_sync(
            lambda: stripe.checkout.Session.retrieve(payment.provider_payment_id, **options)
        )
    except stripe.error.StripeError:
        return None
    data = pay._stripe_object_to_dict(checkout)
    if data.get("status") != "open" or not data.get("url"):
        return None
    return {
        "url": data["url"],
        "expires_at": payment.expires_at,
        "mode": "guarantee" if payment.purpose == "guarantee" else "full",
        "payment_id": payment.id,
        "sent_by_sms": False,
        "sent_by_email": False,
    }


# --------------------------------------------------------------------------- webhooks


async def handle_intent_event(
    session: AsyncSession, tenant_slug: str, intent: dict, event_type: str
) -> bool:
    """``payment_intent.amount_capturable_updated`` / ``succeeded`` d'une garantie ou d'un lien.

    Retourne ``True`` si l'evenement concernait une garantie (deja traite ici), ``False`` si c'est
    un paiement classique que l'appelant doit finaliser.
    """
    metadata = dict(intent.get("metadata") or {})
    if metadata.get("purpose") != "guarantee":
        # Paiement classique : on rattache quand meme l'id du PaymentIntent a un lien Checkout.
        if event_type == "payment_intent.succeeded":
            await find_payment_for_intent(session, intent)
        return False

    payment = await find_payment_for_intent(session, intent)
    if payment is None:
        raise AppError("PAYMENT_NOT_FOUND", "Payment not found", 404)
    order = await session.get(Order, payment.order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    await finalize_guarantee(
        session,
        tenant_slug,
        payment,
        order,
        source="webhook",
        stripe_payment_intent=intent,
    )
    return True


async def handle_checkout_session(session: AsyncSession, tenant_slug: str, obj: dict, event_type: str) -> None:
    """``checkout.session.completed`` / ``expired`` d'un lien de paiement."""
    metadata = dict(obj.get("metadata") or {})
    try:
        payment_id = int(metadata.get("payment_id") or 0)
    except (TypeError, ValueError):
        payment_id = 0
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None:
        logger.warning("checkout session %s without matching payment (tenant=%s)", obj.get("id"), tenant_slug)
        return

    if event_type == "checkout.session.expired":
        if payment.status == "pending":
            payment.status = "expired"
            await session.commit()
        return

    intent_id = obj.get("payment_intent")
    if not intent_id:
        return
    if (payment.provider_payment_id or "").startswith("cs_"):
        payment.provider_payment_id = str(intent_id)
        await session.commit()
    order = await session.get(Order, payment.order_id)
    if order is None:
        return
    if payment.status in _SETTLED_STATUSES:
        return

    options = _stripe_options(payment)
    try:
        intent = pay._stripe_object_to_dict(
            await anyio.to_thread.run_sync(lambda: stripe.PaymentIntent.retrieve(intent_id, **options))
        )
    except Exception as exc:
        raise AppError("STRIPE_PAYMENT_VERIFY_FAILED", pay._safe_stripe_message(exc), 502) from exc

    if payment.purpose == "guarantee":
        await finalize_guarantee(
            session, tenant_slug, payment, order, source="checkout", stripe_payment_intent=intent
        )
    else:
        # Lien « paiement complet » : payment_intent.succeeded finalise (un seul chemin, pas de
        # course entre deux evenements) ; ici on ne fait que rattacher le PaymentIntent.
        await session.commit()


async def handle_guarantee_canceled(session: AsyncSession, payment: Payment) -> bool:
    """``payment_intent.canceled`` sur une empreinte active : le blocage a expire (7 jours) ou a
    ete annule dans Stripe. Retourne ``True`` si l'evenement a ete traite ici."""
    if payment.purpose != "guarantee" or payment.status != "authorized":
        return False
    payment.status = "released"
    payment.settled_at = _now()
    payment.settlement_note = "released_by_stripe"
    order = await session.get(Order, payment.order_id)
    if order is not None and order.payment_status == ORDER_PS_GUARANTEED:
        order.payment_status = ORDER_PS_RELEASED
    await session.commit()
    logger.warning("guarantee released by Stripe: payment_id=%s order_id=%s", payment.id, payment.order_id)
    return True
