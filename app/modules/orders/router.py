from arq import ArqRedis
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from slowapi.util import get_remote_address

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, get_current_user, get_optional_user, get_pagination, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.orders import service
from app.modules.orders.schemas import (
    OrderCreate,
    OrderDetailOut,
    OrderListOut,
    OrderOut,
    OrderReceiptOut,
    OrderStatusUpdate,
    ReorderOut,
)

router = APIRouter()


def _order_rate_limit_key(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    tenant_slug = getattr(request.state, "tenant_slug", None)
    if user_id:
        return f"user:{tenant_slug}:{user_id}"
    return f"ip:{get_remote_address(request)}"


@router.post("", response_model=OrderOut, status_code=201)
@limiter.limit("30/minute", key_func=_order_rate_limit_key)
async def create_order(
    request: Request,
    body: OrderCreate,
    current_user: dict | None = Depends(get_optional_user),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    x_tenant_slug: str | None = Header(default=None, alias="x-tenant-slug"),
):
    """Crée une commande pour un utilisateur authentifié ou un invité.

    Pour les commandes invités (sans JWT) :
    - L'en-tête ``X-Tenant-Slug`` est requis pour identifier le restaurant.
    - Le champ ``customer_email`` dans le body est requis pour joindre le client.
    - Les codes promo ne sont pas acceptés pour les invités.

    Pour les utilisateurs authentifiés, ``X-Tenant-Slug`` est ignoré (tenant_slug
    vient du JWT).
    """
    if current_user is not None:
        tenant_slug = current_user["tenant_slug"]
        user_id: int | None = int(current_user["id"])
    else:
        # Commande invité
        if not x_tenant_slug:
            raise AppError(
                "MISSING_TENANT_SLUG",
                "L'en-tête X-Tenant-Slug est requis pour les commandes sans compte.",
                400,
            )
        if not body.customer_email:
            raise AppError(
                "CUSTOMER_EMAIL_REQUIRED",
                "Le champ customer_email est requis pour les commandes sans compte.",
                422,
                "customer_email",
            )
        tenant_slug = x_tenant_slug
        user_id = None

    async with get_tenant_session(tenant_slug) as session:
        return await service.create_order(
            session,
            body,
            user_id,
            tenant_slug=tenant_slug,
            idempotency_key=idempotency_key,
        )


@router.get("/me", response_model=PaginatedResponse[OrderListOut])
async def list_my_orders(
    current_user=Depends(get_current_user),
    pagination: PaginationParams = Depends(get_pagination),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_my_orders(session, pagination, int(current_user["id"]))
    return PaginatedResponse.build(items, total, pagination)


@router.get("", response_model=PaginatedResponse[OrderListOut])
async def list_orders(
    current_user=Depends(require_role("staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
    status: str | None = Query(None, description="Comma-separated statuses"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        statuses = [part.strip() for part in status.split(",") if part.strip()] if status else None
        items, total = await service.list_orders(session, pagination, statuses, date_from, date_to)
    return PaginatedResponse.build(items, total, pagination)


@router.get("/{order_id}", response_model=OrderDetailOut)
async def get_order_detail(order_id: int, current_user=Depends(get_current_user)):
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_order_detail(
            session,
            order_id,
            user_id=int(current_user["id"]),
            is_staff=role in {"staff", "admin"},
        )


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_my_order(
    order_id: int,
    current_user=Depends(get_current_user),
    arq_pool: ArqRedis = Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.cancel_my_order(
            session,
            order_id,
            int(current_user["id"]),
            current_user["tenant_slug"],
            arq_pool,
        )


@router.post("/{order_id}/reorder", response_model=ReorderOut)
async def reorder(order_id: int, current_user=Depends(get_current_user)):
    role = current_user.get("role")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.build_reorder_payload(
            session,
            order_id,
            user_id=int(current_user["id"]),
            is_staff=role in {"staff", "admin"},
        )


@router.get("/{order_id}/receipt", response_model=OrderReceiptOut)
async def get_receipt(order_id: int, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.build_receipt(session, order_id)


@router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(
    order_id: int,
    body: OrderStatusUpdate,
    current_user=Depends(require_role("staff", "admin")),
    arq_pool: ArqRedis = Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_status(
            session,
            order_id,
            body.status,
            body.note,
            current_user["tenant_slug"],
            arq_pool,
            actor_user_id=int(current_user["id"]),
            is_staff=True,
        )
