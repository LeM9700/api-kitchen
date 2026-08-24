import jwt
from jwt.exceptions import PyJWTError as JWTError
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import engine, tenant_schema_name

BYPASS_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password",
    "/api/v1/auth/verify-email",
    "/api/v1/customer/register",
}

CHANGE_PASSWORD_PATHS = {
    "/api/v1/auth/change-password",
    "/api/v1/auth/logout",
    "/api/v1/auth/sessions",
}


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.tenant_id = None
        request.state.tenant_slug = None
        request.state.user_id = None
        request.state.role = None

        if request.url.path not in BYPASS_PATHS:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                try:
                    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
                    # [PERF] Mise en cache du payload pour eviter un second decodage
                    # dans get_current_user (fix double JWT decode).
                    request.state.jwt_payload = payload
                    request.state.tenant_id = payload.get("tenant_id")
                    request.state.tenant_slug = payload.get("tenant_slug")
                    request.state.user_id = payload.get("sub")
                    request.state.role = payload.get("role")

                    if payload.get("must_change_password") and request.url.path not in CHANGE_PASSWORD_PATHS:
                        return JSONResponse(
                            status_code=403,
                            content={
                                "code": "PASSWORD_CHANGE_REQUIRED",
                                "detail": "You must change your password before continuing",
                            },
                        )
                except JWTError:
                    pass

        return await call_next(request)


async def create_tenant_schema(tenant_slug: str) -> None:
    schema = tenant_schema_name(tenant_slug)
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


async def user_belongs_to_tenant(user_id: int, tenant_slug: str, email: str | None) -> bool:
    """Verifie que ``user_id`` appartient bien au tenant ``tenant_slug`` ET que
    son email correspond au claim ``email`` du JWT.

    [SECURITE] Defense en profondeur : un JWT valide (signature correcte) peut
    porter un ``sub`` et un ``tenant_slug`` incoherents entre eux (bug de mint,
    token rejoue apres un changement de contexte serveur...). Les ids
    utilisateur repartant a 1 par schema tenant, une simple verification
    d'existence de ``user_id`` dans le schema ne suffit PAS : un id=1 existe
    presque toujours (le premier utilisateur de n'importe quel tenant), donc
    un token mint pour le tenant A mais reclamant le tenant B passerait quand
    meme ce controle en resolvant vers le VRAI utilisateur 1 de B. Comparer
    l'email du claim (toujours present sur un access token legitime, voir
    ``issue_tokens`` dans app/modules/auth/service.py, et unique par tenant)
    a l'email reel de l'utilisateur cible ferme ce trou. Ce check doit etre
    appele avant toute utilisation de ``current_user`` issue d'un JWT.
    """
    from app.core.database import get_tenant_session
    from app.modules.auth.models import User

    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        return user is not None and email is not None and user.email == email
