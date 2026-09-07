"""Super-admin endpoints.

All routes require the ``super-admin`` role.

Routes:
- POST   /impersonate/{tenant_slug}   -- generate a short-lived impersonation token
- POST   /impersonation/end           -- revoke the current impersonation token
- GET    /impersonation-log           -- cross-tenant audit log for impersonation events
- GET    /tenants/users               -- cross-tenant user listing with filters
- PATCH  /tenants/{tenant_id}/suspend -- suspend or unsuspend a tenant
"""
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text, update
from sqlalchemy.future import select

from app.core.audit.platform_audit import record_platform_audit_event
from app.core.auth.impersonation import create_impersonation_token
from app.core.auth.token_revocation import revoke_jti
from app.core.database import get_public_session, get_tenant_session
from app.core.http.deps import get_client_ip, get_current_user, require_role
from app.core.http.errors import AppError
from app.modules.auth.models import User
from app.modules.super_admin.models import SuperAdminImpersonationSession

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SuspendBody(BaseModel):
    suspend: bool = True


# ---------------------------------------------------------------------------
# POST /impersonate/{tenant_slug}
# ---------------------------------------------------------------------------


@router.post("/impersonate/{tenant_slug}")
async def impersonate(
    tenant_slug: str,
    request: Request,
    current_user: dict = Depends(require_role("super-admin")),
):
    """Generate a short-lived impersonation access token for the given tenant.

    [SECURITE] Token has ``impersonation: True``, ``sub`` = id REEL de l'acteur
    super-admin (jamais un sentinel ``0`` -- voir app.core.auth.impersonation
    pour le bug historique que cela corrige), ``permissions`` minimales
    (IMPERSONATION_PERMISSIONS), ``source_sid`` pointant la session super-admin
    appelante (revoquee => impersonation revoquee) et ``impersonation_id``
    referencant une ligne ``public.super_admin_impersonation_sessions`` --
    SOURCE DE VERITE persistante de la validite du token, independante de
    Redis (voir validate_impersonation_token). Aucun refresh token n'est
    emis -- impersonation non renouvelable.

    [SECURITE] FAIL-CLOSED transactionnel : l'enregistrement persistant ET
    l'audit "impersonation_started" sont ecrits DANS LA MEME transaction,
    avant tout retour de token -- si l'un des deux echoue, l'exception remonte
    et AUCUN token n'est retourne (voir app.core.audit.platform_audit).

    Args:
        tenant_slug: Slug of the tenant to impersonate.
        request: FastAPI request (for IP / User-Agent extraction).
        current_user: Super-admin user dict injected by dependency (doit
            porter sid/auth_version -- une session super-admin complete).

    Returns:
        ``{access_token, token_type, expires_in}``

    Raises:
        AppError: NOT_FOUND (404) si le tenant n'existe pas.
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "") or None

    async with get_public_session() as pub:
        row = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        tenant_id = row.scalar_one_or_none()
        if tenant_id is None:
            raise AppError("NOT_FOUND", "Tenant not found", 404)

        impersonation_id = str(uuid.uuid4())
        token, jti, expires_at, expires_in = create_impersonation_token(
            current_user, tenant_id, tenant_slug, impersonation_id
        )

        pub.add(
            SuperAdminImpersonationSession(
                id=impersonation_id,
                super_admin_id=current_user["id"],
                source_sid=current_user["sid"],
                tenant_slug=tenant_slug,
                expires_at=expires_at,
            )
        )

        # [SECURITE] Fail-closed : si l'un ou l'autre echoue, l'exception
        # remonte et AUCUN token n'est retourne -- une impersonation non
        # tracee, ou sans enregistrement persistant, ne doit jamais exister.
        await record_platform_audit_event(
            pub,
            event_type="impersonation_started",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="tenant",
            target_id=tenant_slug,
            ip_address=ip,
            user_agent=user_agent,
            metadata={"jti": jti, "impersonation_id": impersonation_id},
        )
        await pub.commit()

    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}


@router.post("/impersonation/end", status_code=204)
async def end_impersonation(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Revoke the impersonation token used to call this endpoint.

    [SECURITE] La revocation PERSISTANTE (``revoked_at`` sur
    ``super_admin_impersonation_sessions``) et l'audit "impersonation_ended"
    sont ecrits DANS LA MEME transaction PostgreSQL, avant tout commit -- si
    l'audit echoue, la revocation est annulee avec lui (aucune revocation ni
    audit partiel ne peut subsister). Redis n'est sollicite QU'APRES ce
    commit reussi, en pur accelerateur de defense en profondeur : son echec
    est avale (best-effort) et n'affecte ni la reponse ni l'etat persistant,
    puisque la source de verite (PostgreSQL) est deja actee.

    Raises:
        AppError: BAD_REQUEST (400) si le token courant n'est pas un token
            d'impersonation.
    """
    if not current_user.get("is_impersonation") or not current_user.get("impersonation_id"):
        raise AppError("BAD_REQUEST", "Not an impersonation session", 400)

    now = datetime.now(timezone.utc)

    async with get_public_session() as pub:
        result = await pub.execute(
            update(SuperAdminImpersonationSession)
            .where(
                SuperAdminImpersonationSession.id == current_user["impersonation_id"],
                SuperAdminImpersonationSession.revoked_at.is_(None),
            )
            .values(revoked_at=now, reason="ended_by_user")
        )

        # Idempotence : un second appel sur une impersonation deja terminee
        # ne doit pas produire un second evenement d'audit "ended" trompeur.
        if result.rowcount > 0:
            await record_platform_audit_event(
                pub,
                event_type="impersonation_ended",
                actor_super_admin_id=current_user.get("impersonated_by_super_admin_id"),
                actor_email=current_user.get("impersonated_by_email"),
                target_type="tenant",
                target_id=current_user.get("tenant_slug"),
                ip_address=get_client_ip(request),
                user_agent=request.headers.get("user-agent", "") or None,
                metadata={
                    "impersonation_id": current_user["impersonation_id"],
                    "jti": current_user.get("jti"),
                },
            )
        await pub.commit()

    # [SECURITE] Best-effort APRES commit : la verite persistante est deja
    # ecrite ; Redis n'est qu'un accelerateur de revocation immediate du jti,
    # jamais requis pour que la revocation soit effective.
    redis = getattr(request.app.state, "arq_pool", None)
    if redis and current_user.get("jti") and current_user.get("exp"):
        try:
            expires_at = datetime.fromtimestamp(current_user["exp"], tz=timezone.utc)
            await revoke_jti(redis, current_user["jti"], expires_at)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GET /impersonation-log
# ---------------------------------------------------------------------------


@router.get("/impersonation-log")
async def impersonation_log(
    request: Request,
    tenant_slug: str | None = Query(None, description="Filter by tenant slug"),
    current_user: dict = Depends(require_role("super-admin")),
):
    """Return impersonation audit log entries from the central platform audit log.

    [SECURITE] Lit ``public.platform_audit_logs`` -- remplace le scatter/gather
    historique sur ``tenant_config_audits`` de chaque schema tenant, qui ne
    tracait jamais le VRAI acteur (``changed_by_user_id`` etait un litteral
    ``0`` code en dur). La consultation elle-meme est une "consultation
    multi-tenant" et est donc auditee, en FAIL-CLOSED comme toute action
    privilegiee de ce module.

    Args:
        request: FastAPI request.
        tenant_slug: Optional filter to restrict results to a single tenant.
        current_user: Super-admin user dict injected by dependency.

    Returns:
        List of audit entries sorted by ``created_at`` descending (max 100).
    """
    query = (
        "SELECT event_type, actor_super_admin_id, actor_email, "
        "target_id AS tenant_slug, ip_address, user_agent, metadata, created_at "
        "FROM public.platform_audit_logs "
        "WHERE event_type IN ('impersonation_started', 'impersonation_ended')"
    )
    params: dict = {}
    if tenant_slug:
        query += " AND target_id = :tenant_slug"
        params["tenant_slug"] = tenant_slug
    query += " ORDER BY created_at DESC LIMIT 100"

    async with get_public_session() as pub:
        rows = await pub.execute(text(query), params)
        results = [dict(r._mapping) for r in rows]

        await record_platform_audit_event(
            pub,
            event_type="cross_tenant_view",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="impersonation_log",
            target_id=tenant_slug,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "") or None,
        )
        await pub.commit()

    return results


# ---------------------------------------------------------------------------
# GET /tenants/users  -- must be registered BEFORE /tenants/{tenant_id}/...
# ---------------------------------------------------------------------------


@router.get("/tenants/users")
async def cross_tenant_users(
    request: Request,
    tenant_slug: str | None = Query(None, description="Filter by tenant slug"),
    role: str | None = Query(None, description="Filter by role"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    email_verified: bool | None = Query(None, description="Filter by email verification"),
    current_user: dict = Depends(require_role("super-admin")),
):
    """List users across all tenant schemas with optional filters.

    Uses ``asyncio.gather`` for parallel per-tenant queries.
    Each tenant is limited to 100 results to guard against huge datasets.

    Args:
        request: FastAPI request.
        tenant_slug: Optional filter to restrict to a single tenant.
        role: Optional role filter (e.g. ``"admin"``, ``"customer"``).
        is_active: Optional active status filter.
        email_verified: Optional email verification filter.
        current_user: Super-admin user dict injected by dependency.

    Returns:
        Flat list of user dicts annotated with ``tenant_slug``.
    """
    async with get_public_session() as pub:
        slugs_result = await pub.execute(text("SELECT slug FROM public.tenants"))
        slugs = [r[0] for r in slugs_result]

    if tenant_slug:
        slugs = [s for s in slugs if s == tenant_slug]

    async def _fetch_users(slug: str) -> list[dict]:
        try:
            async with get_tenant_session(slug) as session:
                stmt = select(User)
                if role is not None:
                    stmt = stmt.where(User.role == role)
                if is_active is not None:
                    stmt = stmt.where(User.is_active.is_(is_active))
                if email_verified is not None:
                    if email_verified:
                        stmt = stmt.where(User.email_verified_at.isnot(None))
                    else:
                        stmt = stmt.where(User.email_verified_at.is_(None))
                result = await session.execute(stmt.limit(100))
                return [
                    {
                        "tenant_slug": slug,
                        "id": u.id,
                        "email": u.email,
                        "role": u.role,
                        "is_active": u.is_active,
                        "email_verified": u.email_verified_at is not None,
                        "created_at": u.created_at,
                    }
                    for u in result.scalars()
                ]
        except Exception:
            return []

    all_results = await asyncio.gather(*[_fetch_users(s) for s in slugs])
    users: list[dict] = []
    for items in all_results:
        users.extend(items)

    # [SECURITE] "Consultation multi-tenant" -- FAIL-CLOSED : si l'audit
    # echoue, la liste n'est pas retournee (voir app.core.audit.platform_audit).
    async with get_public_session() as pub:
        await record_platform_audit_event(
            pub,
            event_type="cross_tenant_view",
            actor_super_admin_id=current_user["id"],
            actor_email=current_user["email"],
            target_type="tenants_users",
            target_id=tenant_slug,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", "") or None,
            metadata={"result_count": len(users)},
        )
        await pub.commit()

    return users


# NOTE: PATCH /tenants/{tenant_id}/suspend and /unsuspend are handled by lifecycle_router.
