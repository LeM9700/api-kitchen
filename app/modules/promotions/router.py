import time
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, Query, Request, status
from slowapi.util import get_remote_address
from sqlalchemy import text

from app.core.database import get_public_session, get_tenant_session
from app.core.http.deps import get_current_user, get_pagination, require_role
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.promotions import service
from app.modules.promotions.schemas import (
    PromotionAdminOut,
    PromotionCampaignCreate,
    PromotionCampaignOut,
    PromotionCleanupOut,
    PromotionCreate,
    PromotionOut,
    PromotionPublicOut,
    PromotionSuperAdminStatsOut,
    PromotionToggleRequest,
    PromotionUpdate,
    PromotionUsageOut,
    PromotionValidateOut,
    PromotionValidateRequest,
)

router = APIRouter()

VALIDATE_MIN_SECONDS = 0.15


def _validate_user_key(request: Request) -> str:
    tenant_slug = getattr(request.state, "tenant_slug", None)
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{tenant_slug}:{user_id}"
    return f"ip:{get_remote_address(request)}"


def _validate_ip_key(request: Request) -> str:
    return f"ip:{get_remote_address(request)}"


async def _sleep_min_duration(started_at: float) -> None:
    elapsed = time.perf_counter() - started_at
    remaining = VALIDATE_MIN_SECONDS - elapsed
    if remaining > 0:
        await anyio.sleep(remaining)


@router.get("", response_model=list[PromotionPublicOut])
async def list_promos(request: Request) -> list[PromotionPublicOut]:
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.list_active(session)


@router.post("/validate", response_model=PromotionValidateOut)
@limiter.limit("3/minute", key_func=_validate_ip_key)
@limiter.limit("5/minute", key_func=_validate_user_key)
async def validate(
    request: Request,
    body: PromotionValidateRequest,
    current_user: dict = Depends(get_current_user),
) -> PromotionValidateOut:
    started_at = time.perf_counter()
    try:
        order_total = body.order_total
        if order_total is None:
            order_total = sum(item.total for item in body.items)
        async with get_tenant_session(current_user["tenant_slug"]) as session:
            return await service.preview_promos(
                session,
                body.normalized_codes(),
                float(order_total),
                body.items,
                user_id=int(current_user["id"]),
            )
    finally:
        await _sleep_min_duration(started_at)


@router.get("/super-admin/stats", response_model=list[PromotionSuperAdminStatsOut])
async def super_admin_stats(
    current_user: dict = Depends(require_role("super-admin")),
    tenant_slug: str | None = Query(None, description="Filter by tenant slug"),
) -> list[PromotionSuperAdminStatsOut]:
    async with get_public_session() as public:
        result = await public.execute(text("SELECT slug FROM public.tenants ORDER BY slug"))
        slugs = [row[0] for row in result]
    if tenant_slug:
        slugs = [slug for slug in slugs if slug == tenant_slug]

    stats: list[PromotionSuperAdminStatsOut] = []
    for slug in slugs:
        async with get_tenant_session(slug) as session:
            stats.append(await service.super_admin_stats_for_tenant(session, slug))
    return stats


@router.get("/admin", response_model=PaginatedResponse[PromotionAdminOut])
async def list_admin_promotions(
    current_user: dict = Depends(require_role("staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
    is_active: bool | None = None,
    campaign_id: int | None = None,
    code: str | None = None,
    user_id: int | None = None,
    is_public: bool | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> PaginatedResponse[PromotionAdminOut]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_admin(
            session,
            pagination,
            is_active=is_active,
            campaign_id=campaign_id,
            code=code,
            user_id=user_id,
            is_public=is_public,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    return PaginatedResponse.build(items, total, pagination)


@router.post("/bulk-generate", response_model=PromotionCampaignOut, status_code=status.HTTP_201_CREATED)
async def bulk_generate(
    body: PromotionCampaignCreate,
    current_user: dict = Depends(require_role("admin")),
) -> PromotionCampaignOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.bulk_generate(session, body, int(current_user["id"]))


@router.post("", response_model=PromotionOut, status_code=status.HTTP_201_CREATED)
async def create_promo(
    body: PromotionCreate,
    current_user: dict = Depends(require_role("admin")),
) -> PromotionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_promotion(session, body)


@router.post("/cleanup-expired", response_model=PromotionCleanupOut)
async def cleanup_expired(
    current_user: dict = Depends(require_role("admin")),
) -> PromotionCleanupOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.cleanup_expired_promotions(session)


@router.get("/{promo_id}", response_model=PromotionAdminOut)
async def get_promo(
    promo_id: int,
    current_user: dict = Depends(require_role("staff", "admin")),
) -> PromotionAdminOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_promotion(session, promo_id)


@router.get("/{promo_id}/usages", response_model=PaginatedResponse[PromotionUsageOut])
async def get_promo_usages(
    promo_id: int,
    current_user: dict = Depends(require_role("staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
) -> PaginatedResponse[PromotionUsageOut]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_usages(session, promo_id, pagination)
    return PaginatedResponse.build(items, total, pagination)


@router.patch("/{promo_id}", response_model=PromotionOut)
async def update_promo(
    promo_id: int,
    body: PromotionUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> PromotionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_promotion(session, promo_id, body)


@router.post("/{promo_id}/toggle", response_model=PromotionOut)
async def toggle_promo(
    promo_id: int,
    body: PromotionToggleRequest,
    current_user: dict = Depends(require_role("admin")),
) -> PromotionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.toggle_promotion(session, promo_id, body.is_active)


@router.delete("/{promo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo(
    promo_id: int,
    current_user: dict = Depends(require_role("admin")),
) -> None:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_promotion(session, promo_id)
