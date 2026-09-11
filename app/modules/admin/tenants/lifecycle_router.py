import asyncio
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text

from app.core.audit.platform_audit import record_platform_audit_event
from app.core.auth.security import get_password_hash
from app.core.database import (
    NEW_TENANT_SLUG_RE,
    TENANT_SLUG_MAX_LENGTH_FOR_CREATION,
    get_public_session,
    get_tenant_session,
)
from app.core.email.resend_service import send_tenant_suspended, send_tenant_unsuspended
from app.core.http.deps import get_arq_pool, get_client_ip, require_role
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
    request: Request,
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

    [🔒 SÉCURITÉ] L'audit ``tenant_created`` est écrit via le paramètre
    ``on_provisioned`` de ``provision_tenant`` -- DANS LA MÊME transaction
    PostgreSQL que l'insertion ``public.tenants``, la création du schéma, des
    tables et du premier admin. Si cet audit échoue, toute la transaction est
    annulée par PostgreSQL (DDL transactionnel) : aucun tenant, aucun schéma,
    aucun admin ne subsiste (voir tests/test_tenant_creation_audit_atomicity.py).
    L'inscription self-service standard (``app/modules/auth/service.py::register``)
    n'a pas d'acteur super-admin à journaliser et n'utilise jamais ce paramètre.

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
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "") or None

    async def _audit_tenant_created(session, provisioned) -> None:
        await record_platform_audit_event(
            session,
            event_type="tenant_created",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="tenant",
            target_id=provisioned.tenant_slug,
            ip_address=ip,
            user_agent=user_agent,
        )

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
        on_provisioned=_audit_tenant_created,
    )

    return TenantCreateResponse(
        id=provisioned.tenant_id,
        slug=provisioned.tenant_slug,
        name=body.name,
        plan=body.plan,
        admin_email=str(body.admin_email),
        temporary_password=temp_password,
    )


@router.patch("/tenants/{tenant_id}/plan")
async def update_tenant_plan(
    tenant_id: int,
    plan: str,
    current_user=Depends(require_role("super-admin")),
):
    """PATCH /admin/tenants/{id}/plan — modifie le plan d'un tenant.

    Args:
        tenant_id: ID du tenant dans public.tenants.
        plan: Nouveau plan (starter, pro, enterprise).
        current_user: Super-admin injecté par dépendance.

    Returns:
        Tenant mis à jour.

    Raises:
        HTTPException: 404 si tenant introuvable, 422 si plan invalide.
    """
    valid_plans = {"starter", "pro", "enterprise"}
    if plan not in valid_plans:
        raise HTTPException(status_code=422, detail=f"Plan invalide. Valeurs acceptées : {valid_plans}")

    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT id FROM public.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Tenant introuvable.")

        await session.execute(
            text("UPDATE public.tenants SET plan = :plan WHERE id = :id"),
            {"plan": plan, "id": tenant_id},
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
        row = result2.fetchone()

    return dict(row._mapping)


@router.patch("/tenants/{tenant_id}/suspend", response_model=TenantResponse)
async def suspend_tenant(
    tenant_id: int,
    body: TenantSuspendRequest,
    request: Request,
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
        # [SECURITE] FAIL-CLOSED : meme transaction que l'UPDATE ci-dessus --
        # si l'audit echoue, le rollback implicite annule aussi la suspension.
        await record_platform_audit_event(
            session,
            event_type="tenant_suspended",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="tenant",
            target_id=tenant_slug,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "") or None,
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

    # Notification email à l'admin tenant (non-bloquante)
    try:
        async with get_tenant_session(tenant_slug) as t_session:
            admin_row = await t_session.execute(
                text("SELECT email FROM users WHERE role = 'admin' AND is_active = true LIMIT 1")
            )
            admin = admin_row.mappings().first()
        if admin:
            asyncio.ensure_future(
                send_tenant_suspended(
                    admin_email=admin["email"],
                    tenant_name=dict(tenant_row._mapping)["name"],
                    reason=body.suspension_message or "Aucune raison spécifiée.",
                )
            )
    except Exception:
        pass

    return TenantResponse(**dict(tenant_row._mapping))


@router.patch("/tenants/{tenant_id}/unsuspend", response_model=TenantResponse)
async def unsuspend_tenant(
    tenant_id: int,
    request: Request,
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
        # [SECURITE] FAIL-CLOSED : meme transaction que l'UPDATE ci-dessus.
        await record_platform_audit_event(
            session,
            event_type="tenant_unsuspended",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="tenant",
            target_id=tenant_slug,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "") or None,
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

    # Notification email à l'admin tenant (non-bloquante)
    try:
        async with get_tenant_session(tenant_slug) as t_session:
            admin_row = await t_session.execute(
                text("SELECT email FROM users WHERE role = 'admin' AND is_active = true LIMIT 1")
            )
            admin = admin_row.mappings().first()
        if admin:
            asyncio.ensure_future(
                send_tenant_unsuspended(
                    admin_email=admin["email"],
                    tenant_name=dict(tenant_row._mapping)["name"],
                )
            )
    except Exception:
        pass

    return TenantResponse(**dict(tenant_row._mapping))
