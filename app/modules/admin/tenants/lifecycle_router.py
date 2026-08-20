import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
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
from app.modules.admin.users.schemas import AdminUserCreate

router = APIRouter()


class TenantCreate(BaseModel):
    slug: str
    name: str
    plan: str = "starter"
    admin_email: EmailStr
    admin_password: str | None = None


class TenantCreateResponse(BaseModel):
    id: int
    slug: str
    name: str
    plan: str
    admin_email: str
    temporary_password: str


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


@router.post("/tenants", response_model=TenantCreateResponse, status_code=201)
async def create_tenant(
    body: TenantCreate,
    current_user=Depends(require_role("super-admin")),
):
    """Crée un tenant (schéma Postgres) et son premier utilisateur admin.

    [🔒 SÉCURITÉ] La temporary_password n'est retournée qu'une seule fois dans
    cette réponse — elle doit être transmise à l'administrateur hors-bande.
    Le compte est marqué must_change_password=True.

    Args:
        body: Slug, name, plan, admin_email, admin_password (optionnel).
        current_user: Super-admin injecté par dépendance.

    Returns:
        TenantCreateResponse avec le mot de passe temporaire de l'admin.
    """
    # Génère le mot de passe si non fourni
    temp_password = body.admin_password or secrets.token_urlsafe(12)

    # 1. Crée la ligne dans public.tenants
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, :plan) RETURNING id, slug"
            ),
            {"slug": body.slug, "name": body.name, "plan": body.plan},
        )
        row = result.fetchone()
        await session.commit()
        tenant_id = row.id
        tenant_slug = row.slug

    # 2. Crée le schéma Postgres + toutes les tables du tenant
    await create_tenant_schema(tenant_slug)

    # 3. Crée le premier admin dans le schéma tenant
    from app.modules.admin.users import service as users_service

    admin_body = AdminUserCreate(
        email=str(body.admin_email),
        full_name=None,
        role="admin",
    )
    # On force le mot de passe généré via une version interne de create_user
    from datetime import datetime, timezone
    from app.core.database import get_tenant_session
    from app.core.auth.security import get_password_hash
    from app.modules.auth.models import User

    async with get_tenant_session(tenant_slug) as session:
        user = User(
            email=str(body.admin_email),
            full_name=None,
            password_hash=get_password_hash(temp_password),
            role="admin",
            permissions=None,
            must_change_password=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        session.add(user)
        await session.commit()

    return TenantCreateResponse(
        id=tenant_id,
        slug=tenant_slug,
        name=body.name,
        plan=body.plan,
        admin_email=str(body.admin_email),
        temporary_password=temp_password,
    )


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
