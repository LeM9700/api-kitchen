"""Router super-admin — authentification dédiée, indépendante du système tenant.

Routes:
- POST /super-admin/login  -- connexion super-admin (email + password → JWT)

[🔒 SÉCURITÉ]
- Endpoint rate-limité à 5 tentatives/minute pour résister au brute-force.
- JWT émis sans tenant_slug ni tenant_id (champs None/0) — rôle = "super-admin".
- Timing constant via DUMMY_HASH même si l'email est introuvable (timing oracle).
- last_login_at mis à jour de façon non-bloquante après émission du token.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Request
from sqlalchemy import text, update

from app.core.auth.security import (
    DUMMY_HASH,
    create_access_token,
    get_password_hash,
    verify_password,
)
from app.core.config import settings
from app.core.database import get_public_session
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.super_admin.schemas import SuperAdminLoginRequest, SuperAdminTokenResponse

router = APIRouter()

# Durée de vie du token super-admin : même valeur que les access tokens standard.
_TOKEN_TTL_SECONDS = settings.jwt_access_expire_minutes * 60


@router.post(
    "/login",
    response_model=SuperAdminTokenResponse,
    summary="Connexion super-admin",
    description=(
        "Authentification dédiée super-admin — indépendante du système tenant. "
        "Retourne un JWT avec role='super-admin' sans tenant_slug."
    ),
)
@limiter.limit("5/minute")
async def super_admin_login(
    request: Request,
    body: SuperAdminLoginRequest,
) -> SuperAdminTokenResponse:
    """POST /super-admin/login — connexion super-admin.

    [🔒 SÉCURITÉ] Timing constant : DUMMY_HASH est vérifié même si l'email
    n'existe pas, pour éviter l'énumération de comptes via timing oracle.

    Args:
        request: Requête FastAPI (requis par SlowAPI).
        body: Email + password du super-admin.

    Returns:
        SuperAdminTokenResponse avec access_token JWT.

    Raises:
        AppError: UNAUTHORIZED (401) si credentials invalides ou compte inactif.
    """
    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT id, email, password_hash, is_active "
                "FROM public.super_admins WHERE email = :email"
            ),
            {"email": body.email},
        )
        admin = row.mappings().first()

    # [🔒 SÉCURITÉ] Toujours vérifier un hash (même dummy) pour égaliser le temps.
    stored_hash = admin["password_hash"] if admin else DUMMY_HASH
    password_ok = verify_password(body.password, stored_hash)

    if not admin or not password_ok:
        raise AppError("UNAUTHORIZED", "Identifiants invalides.", 401)

    if not admin["is_active"]:
        raise AppError("UNAUTHORIZED", "Ce compte super-admin est désactivé.", 401)

    # Émission du JWT super-admin
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(admin["id"]),
        "email": admin["email"],
        "role": "super-admin",
        "tenant_slug": None,
        "tenant_id": None,
        "exp": expire,
        "type": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

    # Mise à jour last_login_at (non-bloquante — l'échec ne doit pas empêcher le login)
    try:
        async with get_public_session() as session:
            await session.execute(
                text(
                    "UPDATE public.super_admins SET last_login_at = :now WHERE id = :id"
                ),
                {"now": now, "id": admin["id"]},
            )
            await session.commit()
    except Exception:
        pass

    return SuperAdminTokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=_TOKEN_TTL_SECONDS,
    )
