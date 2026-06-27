from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import get_public_session, get_tenant_session
from app.core.http.deps import get_arq_pool, require_role
from app.core.tenancy.tenant import create_tenant_schema
from app.modules.admin.tenants import service as tenant_service
from app.modules.admin.tenants.schemas import (
    TenantConfigUpdate,
    TenantResponse,
    TenantSuspendRequest,
)

router = APIRouter()


class TenantCreate(BaseModel):
    slug: str
    name: str
    plan: str = "starter"


@router.get("/tenants")
async def list_tenants(current_user=Depends(require_role("super-admin"))):
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants"
            )
        )
        return [dict(row._mapping) for row in result]


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, current_user=Depends(require_role("super-admin"))):
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, :plan) RETURNING id, slug"
            ),
            body.model_dump(),
        )
        row = result.fetchone()
        await session.commit()
    await create_tenant_schema(body.slug)
    return {"id": row.id, "slug": row.slug}


@router.patch("/tenants/{tenant_id}/suspend", response_model=TenantResponse)
async def suspend_tenant(
    tenant_id: int,
    body: TenantSuspendRequest,
    current_user=Depends(require_role("super-admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantResponse:
    now = datetime.now(timezone.utc)

    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT slug FROM public.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
        row = result.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Tenant introuvable.")

        tenant_slug = row.slug
        await session.execute(
            text(
                "UPDATE public.tenants SET is_suspended = true, "
                "suspended_at = :now, suspension_message = :msg "
                "WHERE id = :id"
            ),
            {"now": now, "msg": body.suspension_message, "id": tenant_id},
        )
        await session.commit()

        result2 = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        tenant_row = result2.fetchone()

    async with get_tenant_session(tenant_slug) as t_session:
        await tenant_service.update_config(
            t_session,
            TenantConfigUpdate(
                is_temporarily_closed=True,
                temporary_closure_message=body.suspension_message,
            ),
            user_id=current_user["id"],
            arq_pool=arq_pool,
            tenant_slug=tenant_slug,
        )

    return TenantResponse(**dict(tenant_row._mapping))


@router.patch("/tenants/{tenant_id}/unsuspend", response_model=TenantResponse)
async def unsuspend_tenant(
    tenant_id: int,
    current_user=Depends(require_role("super-admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantResponse:
    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT slug FROM public.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
        row = result.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Tenant introuvable.")

        tenant_slug = row.slug
        await session.execute(
            text(
                "UPDATE public.tenants SET is_suspended = false, "
                "suspended_at = NULL, suspension_message = NULL "
                "WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        await session.commit()

        result2 = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        tenant_row = result2.fetchone()

    async with get_tenant_session(tenant_slug) as t_session:
        await tenant_service.update_config(
            t_session,
            TenantConfigUpdate(is_temporarily_closed=False),
            user_id=current_user["id"],
            arq_pool=arq_pool,
            tenant_slug=tenant_slug,
        )

    return TenantResponse(**dict(tenant_row._mapping))
