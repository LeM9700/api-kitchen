import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.database import (
    NEW_TENANT_SLUG_RE,
    TENANT_SLUG_MAX_LENGTH_FOR_CREATION,
    get_public_session,
    get_tenant_session,
)
from app.core.http.deps import get_arq_pool, require_role
from app.core.tenancy.provisioning import provision_tenant
from app.modules.admin.tenants import service as tenant_service
from app.modules.admin.tenants.schemas import (
    TenantConfigUpdate,
    TenantResponse,
    TenantSuspendRequest,
)

router = APIRouter()


class TenantCreate(BaseModel):
    # [SECURITE] Meme regle que le flux d'inscription standard (RegisterRequest,
    # app/modules/auth/schemas.py) : sans cette validation, ce parcours acceptait
    # n'importe quelle chaine (longueur et caracteres arbitraires), y compris des
    # slugs susceptibles de faire collision une fois tronques par PostgreSQL.
    slug: str = Field(min_length=1, max_length=TENANT_SLUG_MAX_LENGTH_FOR_CREATION)
    name: str
    plan: str = "starter"
    admin_email: EmailStr
    admin_password: str | None = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not NEW_TENANT_SLUG_RE.fullmatch(v):
            raise ValueError("slug must be lowercase alphanumeric with hyphens/underscores only")
        return v


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
    """Crée un tenant (schéma Postgres, tables applicatives) et son premier
    utilisateur admin -- via le même service de provisioning atomique que
    l'inscription standard (voir app/core/tenancy/provisioning.py), pour que
    les deux parcours produisent des tenants structurellement identiques.

    [🔒 SÉCURITÉ] La temporary_password n'est retournée qu'une seule fois dans
    cette réponse — elle doit être transmise à l'administrateur hors-bande.
    Le compte est marqué must_change_password=True.

    Args:
        body: Slug, name, plan, admin_email, admin_password (optionnel).
        current_user: Super-admin injecté par dépendance.

    Returns:
        TenantCreateResponse avec le mot de passe temporaire de l'admin.

    Raises:
        AppError: TENANT_EXISTS (409) si le slug est déjà pris.
        AppError: TENANT_SCHEMA_COLLISION (409) si un schéma du même nom existe déjà.
    """
    temp_password = body.admin_password or secrets.token_urlsafe(12)

    provisioned = await provision_tenant(
        slug=body.slug,
        name=body.name,
        plan=body.plan,
        admin_fields={
            "email": str(body.admin_email),
            "full_name": None,
            "password_hash": get_password_hash(temp_password),
            "must_change_password": True,
            "email_verified_at": datetime.now(timezone.utc),
        },
    )

    return TenantCreateResponse(
        id=provisioned.tenant_id,
        slug=provisioned.tenant_slug,
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
