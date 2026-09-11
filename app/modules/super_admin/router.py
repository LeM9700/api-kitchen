"""Router super-admin — authentification dédiée, indépendante du système tenant.

Routes:
- POST /super-admin/login              -- connexion (email + password [+ mfa_code])
- POST /super-admin/mfa/setup          -- génère secret TOTP + codes de récupération
- POST /super-admin/mfa/confirm        -- active le MFA après vérification du 1er code
- POST /super-admin/refresh            -- rotation du refresh token
- POST /super-admin/logout             -- révoque la session courante
- POST /super-admin/revoke-all-sessions -- révocation globale (auth_version++)
- POST /super-admin/me/change-password -- changement de mot de passe SA
- GET  /super-admin/tenants/metrics    -- métriques cross-tenant (live)
- POST /super-admin/tenants/{slug}/users/{user_id}/reset-password -- reset password utilisateur tenant
- GET  /super-admin/tenants/{slug}/timeline -- historique des événements d'un tenant

[🔒 SÉCURITÉ]
- Endpoints sensibles rate-limités pour résister au brute-force.
- Un compte actif sans MFA reçoit, avec mot de passe seul, un token
  d'enrôlement restreint (role="super-admin-enrollment") qui n'autorise que
  /mfa/setup et /mfa/confirm — jamais une capacité métier. Dès que le MFA est
  activé, le mot de passe seul ne suffit plus jamais (voir service.login).
- Toute la logique d'authentification vit dans app.modules.super_admin.service
  -- ce router ne fait que l'extraction HTTP (IP, User-Agent, body) et la
  traduction en réponse pour les routes de login/session.
"""

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, field_validator
from sqlalchemy import text

from app.core.auth.security import get_password_hash, verify_password
from app.core.auth.token_revocation import revoke_jti
from app.core.config import settings
from app.core.database import get_public_session, get_tenant_session
from app.core.email.resend_service import send_temp_password_reset
from app.core.http.deps import get_client_ip, get_current_user, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.super_admin import service
from app.modules.super_admin.schemas import (
    SuperAdminLoginRequest,
    SuperAdminMfaConfirmRequest,
    SuperAdminMfaSetupResponse,
    SuperAdminRefreshRequest,
    SuperAdminTokenResponse,
)

router = APIRouter()


def _get_mongo(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.motor_client[settings.mongo_db]


# ─── Schemas locaux ───────────────────────────────────────────────────────────

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_strength(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("Le nouveau mot de passe doit contenir au moins 10 caractères.")
        return v


class TenantMetrics(BaseModel):
    id: int
    slug: str
    name: str
    plan: str
    is_suspended: bool
    is_temporarily_closed: bool
    is_open: bool
    pending_orders: int
    orders_last_24h: int
    revenue_last_24h: float
    live_computed_at: str | None


# ─── POST /login ──────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=SuperAdminTokenResponse,
    summary="Connexion super-admin",
    description=(
        "Authentification dédiée super-admin — indépendante du système tenant. "
        "Un compte sans MFA activé reçoit un token d'enrôlement restreint."
    ),
)
@limiter.limit("5/minute")
async def super_admin_login(
    request: Request,
    body: SuperAdminLoginRequest,
) -> SuperAdminTokenResponse:
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "") or None
    result = await service.login(body, ip, user_agent)
    return SuperAdminTokenResponse(**result)


@router.post(
    "/mfa/setup",
    response_model=SuperAdminMfaSetupResponse,
    summary="Génère un secret TOTP et des codes de récupération",
)
@limiter.limit("10/minute")
async def super_admin_mfa_setup(
    request: Request,
    current_user: dict = Depends(require_role("super-admin", "super-admin-enrollment")),
) -> SuperAdminMfaSetupResponse:
    result = await service.setup_mfa(int(current_user["id"]))
    return SuperAdminMfaSetupResponse(**result)


@router.post(
    "/mfa/confirm",
    summary="Active le MFA après vérification du premier code TOTP",
)
@limiter.limit("10/minute")
async def super_admin_mfa_confirm(
    request: Request,
    body: SuperAdminMfaConfirmRequest,
    current_user: dict = Depends(require_role("super-admin", "super-admin-enrollment")),
) -> dict:
    return await service.confirm_mfa(int(current_user["id"]), body.totp_code)


@router.post(
    "/refresh",
    response_model=SuperAdminTokenResponse,
    summary="Rotation du refresh token super-admin",
)
@limiter.limit("20/minute")
async def super_admin_refresh(
    request: Request,
    body: SuperAdminRefreshRequest,
) -> SuperAdminTokenResponse:
    result = await service.refresh_session(body.refresh_token)
    return SuperAdminTokenResponse(**result)


@router.post("/logout", status_code=204, summary="Révoque la session super-admin courante")
async def super_admin_logout(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> None:
    await service.logout(int(current_user["id"]), current_user.get("sid"))
    redis = getattr(request.app.state, "arq_pool", None)
    if redis and current_user.get("jti") and current_user.get("exp"):
        from datetime import datetime, timezone

        expires_at = datetime.fromtimestamp(current_user["exp"], tz=timezone.utc)
        await revoke_jti(redis, current_user["jti"], expires_at)


@router.post(
    "/revoke-all-sessions",
    status_code=204,
    summary="Révocation globale — invalide toutes les sessions et tokens émis",
)
async def super_admin_revoke_all_sessions(
    request: Request,
    current_user: dict = Depends(require_role("super-admin")),
) -> None:
    await service.revoke_all_sessions(int(current_user["id"]))
    redis = getattr(request.app.state, "arq_pool", None)
    if redis and current_user.get("jti") and current_user.get("exp"):
        from datetime import datetime, timezone

        expires_at = datetime.fromtimestamp(current_user["exp"], tz=timezone.utc)
        await revoke_jti(redis, current_user["jti"], expires_at)


# ─── POST /me/change-password ─────────────────────────────────────────────────

@router.post(
    "/me/change-password",
    status_code=204,
    summary="Changement de mot de passe super-admin",
)
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(require_role("super-admin")),
) -> None:
    """POST /super-admin/me/change-password — change le mot de passe SA.

    [🔒 SÉCURITÉ] Vérifie le mot de passe actuel avant d'accepter le nouveau.
    Utilise bcrypt via get_password_hash.

    Args:
        body: current_password + new_password (min 10 caractères).
        current_user: Super-admin injecté par dépendance JWT.

    Raises:
        AppError: UNAUTHORIZED (401) si current_password incorrect.
    """
    admin_id = int(current_user["id"])

    async with get_public_session() as session:
        row = await session.execute(
            text("SELECT password_hash FROM public.super_admins WHERE id = :id"),
            {"id": admin_id},
        )
        admin = row.mappings().first()

    if not admin:
        raise AppError("NOT_FOUND", "Compte super-admin introuvable.", 404)

    if not verify_password(body.current_password, admin["password_hash"]):
        raise AppError("UNAUTHORIZED", "Mot de passe actuel incorrect.", 401)

    new_hash = get_password_hash(body.new_password)

    async with get_public_session() as session:
        await session.execute(
            text("UPDATE public.super_admins SET password_hash = :hash WHERE id = :id"),
            {"hash": new_hash, "id": admin_id},
        )
        await session.commit()


# ─── GET /tenants/metrics ─────────────────────────────────────────────────────

@router.get(
    "/tenants/metrics",
    response_model=list[TenantMetrics],
    summary="Métriques cross-tenant en temps réel",
)
async def tenants_metrics(
    request: Request,
    current_user: dict = Depends(require_role("super-admin")),
) -> list[TenantMetrics]:
    """GET /super-admin/tenants/metrics — métriques live pour tous les tenants.

    Pour chaque tenant :
    - Lit ``live_dashboard_{slug}`` dans MongoDB (pending_orders, orders_last_24h, revenue_last_24h)
    - Lit ``tenant_config.is_temporarily_closed`` dans le schéma tenant
    - Calcule ``is_open`` = not is_suspended and not is_temporarily_closed

    [⚡ PERF] Toutes les requêtes tenant sont parallélisées via asyncio.gather.

    Args:
        request: FastAPI request (pour accéder à motor_client).
        current_user: Super-admin injecté par dépendance.

    Returns:
        Liste de TenantMetrics triée par slug.
    """
    mongo: AsyncIOMotorDatabase = _get_mongo(request)

    # 1. Récupérer la liste des tenants depuis public.tenants
    async with get_public_session() as pub:
        result = await pub.execute(
            text(
                "SELECT id, slug, name, plan, is_suspended "
                "FROM public.tenants ORDER BY slug"
            )
        )
        tenants = [dict(r._mapping) for r in result]

    async def _fetch_tenant_metrics(t: dict[str, Any]) -> TenantMetrics:
        slug = t["slug"]

        # 2a. Live stats depuis MongoDB (non-bloquant sur erreur)
        live_doc: dict[str, Any] = {}
        try:
            doc = await mongo[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
            if doc:
                live_doc = doc
        except Exception:
            pass

        # 2b. is_temporarily_closed depuis le schéma tenant
        is_closed = False
        try:
            async with get_tenant_session(slug) as session:
                row = await session.execute(
                    text("SELECT is_temporarily_closed FROM tenant_config LIMIT 1")
                )
                cfg = row.mappings().first()
                if cfg:
                    is_closed = bool(cfg["is_temporarily_closed"])
        except Exception:
            pass

        is_open = not t["is_suspended"] and not is_closed

        return TenantMetrics(
            id=t["id"],
            slug=slug,
            name=t["name"],
            plan=t["plan"],
            is_suspended=t["is_suspended"],
            is_temporarily_closed=is_closed,
            is_open=is_open,
            pending_orders=live_doc.get("pending_orders", 0),
            orders_last_24h=live_doc.get("orders_last_24h", 0),
            revenue_last_24h=live_doc.get("revenue_last_24h", 0.0),
            live_computed_at=str(live_doc["computed_at"]) if "computed_at" in live_doc else None,
        )

    results = await asyncio.gather(*[_fetch_tenant_metrics(t) for t in tenants])
    return list(results)


# ─── POST /tenants/{slug}/users/{user_id}/reset-password ─────────────────────

class ResetPasswordResponse(BaseModel):
    user_id: int
    email: str
    temporary_password: str


@router.post(
    "/tenants/{slug}/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    summary="Réinitialiser le mot de passe d'un utilisateur tenant",
)
async def reset_tenant_user_password(
    slug: str,
    user_id: int,
    current_user: dict = Depends(require_role("super-admin")),
) -> ResetPasswordResponse:
    """POST /super-admin/tenants/{slug}/users/{user_id}/reset-password.

    Génère un nouveau mot de passe temporaire pour un utilisateur d'un tenant,
    le notifie par email via Resend, et force le changement à la prochaine connexion.

    [🔒 SÉCURITÉ] Réservé au super-admin. Ne nécessite pas d'impersonner le tenant.
    Le mot de passe est transmis par email — ne pas le retourner dans les logs.

    Args:
        slug: Slug du tenant.
        user_id: ID de l'utilisateur dans le schéma tenant.
        current_user: Super-admin injecté par dépendance.

    Returns:
        ResetPasswordResponse avec temporary_password (affiché une seule fois).

    Raises:
        AppError: NOT_FOUND si tenant ou utilisateur introuvable.
    """
    # Vérifier que le tenant existe
    async with get_public_session() as pub:
        row = await pub.execute(
            text("SELECT name FROM public.tenants WHERE slug = :slug"),
            {"slug": slug},
        )
        tenant = row.mappings().first()
    if not tenant:
        raise AppError("NOT_FOUND", "Tenant introuvable.", 404)

    temp_password = secrets.token_urlsafe(12)

    async with get_tenant_session(slug) as session:
        row = await session.execute(
            text("SELECT id, email FROM users WHERE id = :id AND is_active = true"),
            {"id": user_id},
        )
        user = row.mappings().first()
        if not user:
            raise AppError("NOT_FOUND", "Utilisateur introuvable ou inactif.", 404)

        await session.execute(
            text(
                "UPDATE users SET password_hash = :hash, must_change_password = true "
                "WHERE id = :id"
            ),
            {"hash": get_password_hash(temp_password), "id": user_id},
        )
        await session.commit()

    # Notification email (non-bloquante)
    try:
        asyncio.ensure_future(
            send_temp_password_reset(
                user_email=user["email"],
                tenant_name=tenant["name"],
                temp_password=temp_password,
            )
        )
    except Exception:
        pass

    return ResetPasswordResponse(
        user_id=user_id,
        email=user["email"],
        temporary_password=temp_password,
    )


# ─── GET /tenants/{slug}/timeline ────────────────────────────────────────────

class TimelineEntry(BaseModel):
    id: int
    event_type: str          # "config_change" | "impersonation" | "suspension"
    field_name: str
    old_value: str | None
    new_value: str | None
    actor: str | None        # email de l'acteur
    ip_address: str | None
    changed_at: str


@router.get(
    "/tenants/{slug}/timeline",
    response_model=list[TimelineEntry],
    summary="Timeline des événements d'un tenant",
)
async def get_tenant_timeline(
    slug: str,
    current_user: dict = Depends(require_role("super-admin")),
) -> list[TimelineEntry]:
    """GET /super-admin/tenants/{slug}/timeline — historique des événements.

    Lit ``tenant_config_audits`` dans le schéma tenant pour retourner la
    timeline complète : changements de config, impersonations, fermetures.

    Args:
        slug: Slug du tenant.
        current_user: Super-admin injecté par dépendance.

    Returns:
        Liste d'événements triés par date décroissante (max 200).

    Raises:
        AppError: NOT_FOUND si le tenant est introuvable.
    """
    # Vérifier que le tenant existe
    async with get_public_session() as pub:
        row = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": slug},
        )
        if row.scalar_one_or_none() is None:
            raise AppError("NOT_FOUND", "Tenant introuvable.", 404)

    async with get_tenant_session(slug) as session:
        rows = await session.execute(
            text(
                "SELECT id, field_name, old_value, new_value, "
                "user_email, ip_address, changed_at "
                "FROM tenant_config_audits "
                "ORDER BY changed_at DESC LIMIT 200"
            )
        )
        entries = rows.mappings().all()

    def _event_type(field_name: str) -> str:
        if field_name == "impersonate":
            return "impersonation"
        if field_name in ("is_temporarily_closed", "temporary_closure_message"):
            return "suspension"
        return "config_change"

    return [
        TimelineEntry(
            id=e["id"],
            event_type=_event_type(e["field_name"]),
            field_name=e["field_name"],
            old_value=e["old_value"],
            new_value=e["new_value"],
            actor=e["user_email"],
            ip_address=e["ip_address"],
            changed_at=e["changed_at"].isoformat() if hasattr(e["changed_at"], "isoformat") else str(e["changed_at"]),
        )
        for e in entries
    ]
