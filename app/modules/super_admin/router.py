"""Router super-admin — authentification dédiée, indépendante du système tenant.

Routes:
- POST /super-admin/login        -- connexion (email + password [+ mfa_code])
- POST /super-admin/mfa/setup    -- génère secret TOTP + codes de récupération
- POST /super-admin/mfa/confirm  -- active le MFA après vérification du 1er code
- POST /super-admin/refresh      -- rotation du refresh token
- POST /super-admin/logout       -- révoque la session courante
- POST /super-admin/revoke-all-sessions -- révocation globale (auth_version++)

[🔒 SÉCURITÉ]
- Endpoints sensibles rate-limités pour résister au brute-force.
- Un compte actif sans MFA reçoit, avec mot de passe seul, un token
  d'enrôlement restreint (role="super-admin-enrollment") qui n'autorise que
  /mfa/setup et /mfa/confirm — jamais une capacité métier. Dès que le MFA est
  activé, le mot de passe seul ne suffit plus jamais (voir service.login).
- Toute la logique métier vit dans app.modules.super_admin.service — ce
  router ne fait que l'extraction HTTP (IP, User-Agent, body) et la
  traduction en réponse.
"""

from fastapi import APIRouter, Depends, Request

from app.core.auth.token_revocation import revoke_jti
from app.core.http.deps import get_client_ip, get_current_user, require_role
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
