"""Super-admin endpoints.

All routes require the ``super-admin`` role.

Routes:
- POST   /impersonate/{tenant_slug}   -- generate a short-lived impersonation token
- GET    /impersonation-log           -- cross-tenant audit log for impersonation events
- GET    /tenants/users               -- cross-tenant user listing with filters
- PATCH  /tenants/{tenant_id}/suspend -- suspend or unsuspend a tenant
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
import jwt
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import get_public_session, get_tenant_session
from app.core.http.deps import get_client_ip, require_role
from app.core.http.errors import AppError
from app.modules.auth.models import User

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
    """Generate a 5-minute impersonation access token for the given tenant.

    [SECURITE] Token has ``impersonation: True`` and ``impersonated_by`` claims.
    No refresh token is issued -- impersonation sessions are non-renewable.
    The action is logged to ``tenant_config_audits`` in the tenant schema.

    Args:
        tenant_slug: Slug of the tenant to impersonate.
        request: FastAPI request (for IP / User-Agent extraction).
        current_user: Super-admin user dict injected by dependency.

    Returns:
        ``{access_token, token_type, expires_in}``
    """
    # Verify tenant exists
    async with get_public_session() as pub:
        row = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        if row.scalar_one_or_none() is None:
            raise AppError("NOT_FOUND", "Tenant not found", 404)

    # Build short-lived impersonation token manually (5 min TTL, no refresh)
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=5)
    jti = str(uuid.uuid4())
    payload = {
        "sub": "0",
        "email": f"impersonated@{tenant_slug}",
        "role": "admin",
        "tenant_slug": tenant_slug,
        "tenant_id": 0,
        "impersonated_by": current_user["email"],
        "impersonation": True,
        "exp": expire,
        "type": "access",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

    # Extract request metadata for audit log
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    # Log impersonation to tenant_config_audits (non-blocking -- audit failure
    # must never prevent token issuance)
    # Columns: id, changed_by_user_id, changed_at, field_name, old_value,
    #          new_value, ip_address, user_agent
    try:
        async with get_tenant_session(tenant_slug) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_config_audits "
                    "(changed_by_user_id, field_name, old_value, new_value, "
                    " ip_address, user_agent) "
                    "VALUES (0, 'impersonate', NULL, :actor, :ip, :ua)"
                ),
                {
                    "actor": current_user["email"],
                    "ip": ip,
                    "ua": user_agent,
                },
            )
            await session.commit()
    except Exception:
        pass

    return {"access_token": token, "token_type": "bearer", "expires_in": 300}


# ---------------------------------------------------------------------------
# GET /impersonation-log
# ---------------------------------------------------------------------------


@router.get("/impersonation-log")
async def impersonation_log(
    request: Request,
    tenant_slug: str | None = Query(None, description="Filter by tenant slug"),
    current_user: dict = Depends(require_role("super-admin")),
):
    """Return impersonation audit log entries across all tenants.

    Queries ``tenant_config_audits`` WHERE ``field_name = 'impersonate'`` in
    every tenant schema (or a specific one via ``tenant_slug`` filter).
    Uses ``asyncio.gather`` for parallel queries.

    Args:
        request: FastAPI request.
        tenant_slug: Optional filter to restrict results to a single tenant.
        current_user: Super-admin user dict injected by dependency.

    Returns:
        List of audit entries sorted by ``changed_at`` descending (max 100).
    """
    async with get_public_session() as pub:
        slugs_result = await pub.execute(text("SELECT slug FROM public.tenants"))
        slugs = [r[0] for r in slugs_result]

    if tenant_slug:
        slugs = [s for s in slugs if s == tenant_slug]

    async def _fetch(slug: str) -> list[dict]:
        try:
            async with get_tenant_session(slug) as session:
                rows = await session.execute(
                    text(
                        "SELECT changed_by_user_id, ip_address, "
                        "new_value AS actor, changed_at "
                        "FROM tenant_config_audits "
                        "WHERE field_name = 'impersonate' "
                        "ORDER BY changed_at DESC LIMIT 100"
                    )
                )
                return [
                    {"tenant_slug": slug, **dict(r._mapping)}
                    for r in rows
                ]
        except Exception:
            return []

    all_results = await asyncio.gather(*[_fetch(s) for s in slugs])
    results: list[dict] = []
    for items in all_results:
        results.extend(items)

    results.sort(key=lambda x: x["changed_at"], reverse=True)
    return results[:100]


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
    return users


# NOTE: PATCH /tenants/{tenant_id}/suspend and /unsuspend are handled by lifecycle_router.
