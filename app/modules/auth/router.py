import asyncio

from fastapi import APIRouter, Depends, Request

from app.core.config import settings
from app.core.database import get_tenant_session
from app.core.http.deps import get_client_ip, get_current_user, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.auth import service
from app.modules.auth.login_audit import log_login_event
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    TokenResponse,
    UserOut,
)

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest):
    arq_pool = getattr(request.app.state, "arq_pool", None)
    _, access, refresh, session_id = await service.register(body, arq_pool=arq_pool)
    return TokenResponse(access_token=access, refresh_token=refresh, session_id=session_id)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest):
    ip = get_client_ip(request)
    ua = request.headers.get("user-agent", "") or None
    mongo_client = getattr(request.app.state, "motor_client", None)
    mongo_db = mongo_client[settings.mongo_db] if mongo_client is not None else None

    try:
        user, access, refresh, session_id = await service.login(body)
        if mongo_db is not None:
            asyncio.create_task(
                log_login_event(
                    mongo_db, body.tenant_slug, body.email, user.id,
                    True, None, ip, ua,
                )
            )
        return TokenResponse(access_token=access, refresh_token=refresh, session_id=session_id)
    except AppError as exc:
        if mongo_db is not None:
            asyncio.create_task(
                log_login_event(
                    mongo_db, body.tenant_slug, body.email, None,
                    False, exc.code, ip, ua,
                )
            )
        raise


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(current_user: dict = Depends(require_role("super-admin", "admin"))):
    return await service.setup_mfa(
        current_user["tenant_slug"],
        int(current_user["id"]),
    )


@router.post("/mfa/confirm")
async def mfa_confirm(
    body: MfaVerifyRequest,
    current_user: dict = Depends(require_role("super-admin", "admin")),
):
    return await service.confirm_mfa(
        current_user["tenant_slug"],
        int(current_user["id"]),
        body.totp_code,
    )


@router.post("/mfa/backup-codes/regenerate")
async def mfa_regenerate_backup_codes(
    body: MfaVerifyRequest,
    current_user: dict = Depends(require_role("super-admin", "admin")),
):
    return await service.regenerate_mfa_backup_codes(
        current_user["tenant_slug"],
        int(current_user["id"]),
        body.totp_code,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
async def refresh(request: Request, body: RefreshRequest):
    tokens = await service.refresh_token(body.refresh_token)
    return TokenResponse(**tokens)


@router.post("/logout", status_code=204)
async def logout(request: Request, current_user: dict = Depends(get_current_user)):
    redis = getattr(request.app.state, "arq_pool", None)
    await service.revoke_all_sessions(
        int(current_user["id"]),
        current_user["tenant_slug"],
        revoke_current=True,
        redis=redis,
    )
    # Invalidate current access token JTI so it cannot be reused
    if redis and current_user.get("jti") and current_user.get("exp"):
        from datetime import datetime, timezone

        from app.core.auth.token_revocation import revoke_jti

        expires_at = datetime.fromtimestamp(current_user["exp"], tz=timezone.utc)
        await revoke_jti(redis, current_user["jti"], expires_at)


@router.post("/forgot-password", status_code=202)
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
):
    arq_pool = getattr(request.app.state, "arq_pool", None)
    await service.forgot_password(body, arq_pool=arq_pool)
    return {"message": "If the account exists, a reset code has been sent"}


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, body: ResetPasswordRequest):
    redis = getattr(request.app.state, "arq_pool", None)
    return await service.reset_password(body, redis=redis)


@router.get("/verify-email")
async def verify_email(token: str, tenant_slug: str):
    """Verifie l'adresse email d'un utilisateur a partir du lien recu par email.

    Appelée via le lien envoye a l'inscription.
    Marque email_verified_at sur le user et invalide le token.

    Args:
        token: Token UUID4 de verification (query param).
        tenant_slug: Slug du tenant de l'utilisateur (query param).
    """
    return await service.verify_email(token, tenant_slug)


@router.post("/resend-verification", status_code=202)
@limiter.limit("3/minute")
async def resend_verification(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    arq_pool = getattr(request.app.state, "arq_pool", None)
    await service.resend_verification(
        int(current_user["id"]),
        current_user["tenant_slug"],
        arq_pool=arq_pool,
    )
    return {"message": "Verification email sent"}


@router.post("/change-password")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    session_id: int | None = None,
    current_user: dict = Depends(get_current_user),
):
    redis = getattr(request.app.state, "arq_pool", None)
    return await service.change_password(
        int(current_user["id"]),
        current_user["tenant_slug"],
        body,
        current_refresh_token_id=session_id,
        redis=redis,
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request,
    current_session_id: int | None = None,
    current_user: dict = Depends(get_current_user),
):
    """List all active sessions for the authenticated user.

    Optionally marks the session identified by current_session_id as is_current=True.
    """
    return await service.get_sessions(
        int(current_user["id"]),
        current_user["tenant_slug"],
        current_session_id,
    )


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Revoke a specific session by ID (ownership-checked)."""
    redis = getattr(request.app.state, "arq_pool", None)
    await service.revoke_session(
        session_id,
        int(current_user["id"]),
        current_user["tenant_slug"],
        redis=redis,
    )


@router.delete("/sessions", status_code=204)
async def revoke_all_sessions(
    request: Request,
    revoke_current: bool = False,
    current_session_id: int | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Revoke all sessions.

    If revoke_current=False (default): revoke all sessions except the current one
    (identified by current_session_id).
    If revoke_current=True: revoke ALL sessions including current, and add the
    current access token JTI to the Redis deny-list.
    """
    redis = getattr(request.app.state, "arq_pool", None)
    await service.revoke_all_sessions(
        int(current_user["id"]),
        current_user["tenant_slug"],
        current_session_id=current_session_id,
        revoke_current=revoke_current,
        redis=redis,
    )
    if revoke_current and redis and current_user.get("jti") and current_user.get("exp"):
        from datetime import datetime, timezone

        from app.core.auth.token_revocation import revoke_jti

        expires_at = datetime.fromtimestamp(current_user["exp"], tz=timezone.utc)
        await revoke_jti(redis, current_user["jti"], expires_at)


@router.get("/me", response_model=UserOut)
async def me(current_user: dict = Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        user = await session.get(User, int(current_user["id"]))
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        return UserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            phone=user.phone,
            role=user.role,
            permissions=user.permissions,
            is_active=user.is_active,
            email_verified=user.email_verified_at is not None,
            must_change_password=user.must_change_password,
        )
