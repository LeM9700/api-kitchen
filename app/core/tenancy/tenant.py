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
