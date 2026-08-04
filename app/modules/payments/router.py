from datetime import datetime

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, get_pagination, require_permission, require_role
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.payments import service
from app.modules.payments.schemas import (
    LocalTestPaymentRequest,
    PaymentConfirmRequest,
    PaymentDetailOut,
    PaymentIntentRequest,
    PaymentListItemOut,
    PaymentOut,
    PaymentSummaryOut,
    RefundCreate,
    RefundOut,
    TerminalConnectionTokenOut,
    TerminalPaymentIntentOut,
    TerminalPaymentIntentRequest,
    TerminalReaderActionOut,
    TerminalReaderListOut,
)

stripe.api_key = settings.stripe_secret_key

router = APIRouter()


@router.post("/intent")
async def create_intent(body: PaymentIntentRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await service.create_intent(
            session,
            body.order_id,
            tenant_slug=current_user["tenant_slug"],
            user_id=int(current_user["id"]) if current_user.get("id") is not None else None,
        )
        payment = result["payment"]
        return {
            "client_secret": result["client_secret"],
            "payment": {
                "id": payment.id,
                "order_id": payment.order_id,
                "amount": float(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "provider_payment_id": payment.provider_payment_id,
            },
        }


@router.post("/confirm", response_model=PaymentOut)
async def confirm(body: PaymentConfirmRequest, current_user=Depends(get_current_user)):
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.confirm(
            session,
            body.provider_payment_id,
            tenant_slug=current_user["tenant_slug"],
            user_id=int(current_user["id"]) if current_user.get("id") is not None else None,
            is_staff=role in {"staff", "admin", "super-admin"},
        )


@router.post("/local-test/confirm", response_model=PaymentOut)
async def confirm_local_test_payment(
    body: LocalTestPaymentRequest,
    current_user=Depends(get_current_user),
):
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.confirm_local_test_payment(
            session,
            body.order_id,
            tenant_slug=current_user["tenant_slug"],
            user_id=int(current_user["id"]) if current_user.get("id") is not None else None,
            is_staff=role in {"staff", "admin", "super-admin"},
        )


@router.post("/webhook", status_code=204)
@limiter.limit("60/minute")
async def webhook(request: Request):
    """Handle incoming Stripe webhook events.

    Verifies the Stripe signature via ``service.verify_stripe_webhook_event`` before
    processing any event, trying the platform secret then the Connect secret (direct
    charges on connected accounts have their own signing secret). The raw body must
    be read before any JSON parsing so the HMAC digest matches the original bytes.

    Args:
        request: Raw FastAPI request object.

    Raises:
        HTTPException: 400 if ``stripe-signature`` header is absent or invalid.
    """
    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = service.verify_stripe_webhook_event(
            raw_body,
            sig_header,
            secrets=[
                ("platform", settings.stripe_webhook_secret),
                ("connect", settings.stripe_webhook_connect_secret),
            ],
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    arq_pool = getattr(request.app.state, "arq_pool", None)
    try:
        tenant_slug = await service.extract_tenant_slug_from_event(event, arq_pool=arq_pool)
    except Exception:
        raise HTTPException(status_code=400, detail="Missing tenant metadata")

    async with get_tenant_session(tenant_slug) as session:
        await service.handle_webhook(session, tenant_slug, event)


@router.get("/summary", response_model=PaymentSummaryOut)
async def payment_summary(
    current_user: dict = Depends(require_permission("payments:read", "staff", "admin")),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> PaymentSummaryOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_payment_summary(session, date_from, date_to)


@router.get("", response_model=PaginatedResponse[PaymentListItemOut])
async def list_payments(
    current_user: dict = Depends(require_permission("payments:read", "staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
    status: str | None = Query(None, description="Comma-separated statuses"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    order_id: int | None = None,
    provider: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> PaginatedResponse[PaymentListItemOut]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_payments(
            session,
            pagination,
            status=status,
            date_from=date_from,
            date_to=date_to,
            order_id=order_id,
            provider=provider,
            min_amount=min_amount,
            max_amount=max_amount,
        )
    return PaginatedResponse.build(items, total, pagination)


@router.get("/export/csv", response_class=PlainTextResponse)
async def export_payments_csv(
    current_user: dict = Depends(require_role("admin")),
    status: str | None = Query(None, description="Comma-separated payment statuses"),
    provider: str | None = None,
    payment_status: str | None = Query(None, description="Comma-separated order payment statuses"),
    order_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> PlainTextResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        csv_text = await service.export_payments_csv(
            session,
            status=status,
            provider=provider,
            payment_status=payment_status,
            order_type=order_type,
            date_from=date_from,
            date_to=date_to,
        )
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=payments.csv"},
    )


@router.post("/terminal/connection-token", response_model=TerminalConnectionTokenOut)
async def terminal_connection_token(
    current_user: dict = Depends(require_permission("payments:terminal", "staff", "admin")),
) -> TerminalConnectionTokenOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_terminal_connection_token(session, current_user["tenant_slug"])


@router.post("/terminal/intent", response_model=TerminalPaymentIntentOut)
async def terminal_payment_intent(
    body: TerminalPaymentIntentRequest,
    current_user: dict = Depends(require_permission("payments:terminal", "staff", "admin")),
) -> TerminalPaymentIntentOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_terminal_intent(
            session,
            body.order_id,
            tenant_slug=current_user["tenant_slug"],
            user_id=int(current_user["id"]) if current_user.get("id") is not None else None,
            reader_id=body.reader_id,
            process_on_reader=body.process_on_reader,
        )


@router.get("/terminal/readers", response_model=TerminalReaderListOut)
async def terminal_readers(
    current_user: dict = Depends(require_permission("payments:terminal", "staff", "admin")),
    location_id: str | None = None,
) -> TerminalReaderListOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_terminal_readers(
            session,
            current_user["tenant_slug"],
            location_id=location_id,
        )


@router.post("/terminal/readers/{reader_id}/process", response_model=TerminalReaderActionOut)
async def process_terminal_reader(
    reader_id: str,
    body: PaymentConfirmRequest,
    current_user: dict = Depends(require_permission("payments:terminal", "staff", "admin")),
) -> TerminalReaderActionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.process_terminal_reader(
            session,
            current_user["tenant_slug"],
            reader_id,
            body.provider_payment_id,
        )


@router.post("/terminal/readers/{reader_id}/cancel", response_model=TerminalReaderActionOut)
async def cancel_terminal_reader(
    reader_id: str,
    current_user: dict = Depends(require_permission("payments:terminal", "staff", "admin")),
) -> TerminalReaderActionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.cancel_terminal_reader_action(
            session,
            current_user["tenant_slug"],
            reader_id,
        )


@router.get("/{order_id}", response_model=PaymentDetailOut)
async def get_payment(order_id: int, current_user: dict = Depends(get_current_user)) -> PaymentDetailOut:
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_payment_for_order(
            session,
            order_id,
            user_id=int(current_user["id"]),
            is_staff=role in {"staff", "admin"},
        )


@router.get("/{order_id}/refunds", response_model=list[RefundOut])
async def list_refunds(order_id: int, current_user: dict = Depends(get_current_user)) -> list[RefundOut]:
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_refunds_for_order(
            session,
            order_id,
            user_id=int(current_user["id"]),
            is_staff=role in {"staff", "admin"},
        )


@router.post("/{order_id}/refund", response_model=RefundOut, status_code=201)
@limiter.limit("5/minute")
async def create_refund(
    request: Request,
    order_id: int,
    body: RefundCreate,
    response: Response,
    current_user: dict = Depends(require_role("admin")),
) -> RefundOut:
    """Émet un remboursement Stripe total ou partiel sur une commande.

    Seuls les utilisateurs avec le rôle ``staff`` ou ``admin`` peuvent déclencher
    un remboursement. La commande doit être dans l'état ``cancelled`` ou ``delivered``
    et aucun remboursement préalable ne doit exister pour le paiement associé.

    Args:
        request: Requête FastAPI courante (requis par le rate limiter).
        order_id: Identifiant de la commande à rembourser.
        body: Payload contenant le montant optionnel (centimes) et le motif.
        response: Objet réponse FastAPI pour injecter les headers personnalisés.
        current_user: Utilisateur authentifié avec rôle ``staff`` ou ``admin``.

    Returns:
        ``RefundOut`` avec les détails du remboursement créé, statut HTTP 201.

    Raises:
        AppError: Voir ``payments.service.create_refund`` pour les codes d'erreur.
    """
    response.headers["X-Refund-Currency"] = "EUR"
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_refund(
            session,
            tenant_slug=current_user["tenant_slug"],
            order_id=order_id,
            user_id=current_user["id"],
            amount=body.amount,
            reason=body.reason,
        )
