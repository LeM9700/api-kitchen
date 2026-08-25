from collections.abc import Callable

from arq import ArqRedis
from fastapi import Depends, Query, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError as JWTError

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.core.auth.security import decode_token
from app.core.auth.token_revocation import is_jti_revoked, is_user_disabled

bearer_scheme = HTTPBearer(auto_error=False)

# Paths autorisés même quand must_change_password=True.
# Tout autre endpoint renvoie 403 MUST_CHANGE_PASSWORD.
_MUST_CHANGE_PASSWORD_ALLOWED_PATHS = frozenset({
    "/auth/change-password",
    "/auth/logout",
})


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """Extrait et valide l'utilisateur courant depuis le JWT.

    Consulte d'abord ``request.state.jwt_payload`` -- peuple par ``TenantMiddleware``
    lors du decodage initial -- pour eviter un double decodage cryptographique.
    Si le payload n'est pas disponible (route sans middleware, test direct), decode
    le token normalement comme fallback.

    Args:
        request: Requete FastAPI courante.
        credentials: Credentials HTTP Bearer extraits du header ``Authorization``.

    Returns:
        Dictionnaire utilisateur avec les cles ``id``, ``tenant_id``, ``tenant_slug``,
        ``role``, ``email``.

    Raises:
        AppError: UNAUTHORIZED (401) si le token est absent, invalide, ou n'est pas
            un access token.
    """
    if credentials is None:
        raise AppError("UNAUTHORIZED", "Authentication required", 401)

    # [PERF] Reutilise le payload deja decode par TenantMiddleware si disponible,
    # evitant une seconde verification HMAC-SHA256 par requete.
    payload = getattr(request.state, "jwt_payload", None)
    if payload is None:
        try:
            payload = decode_token(credentials.credentials)
        except JWTError as exc:
            raise AppError("UNAUTHORIZED", "Invalid token", 401) from exc

    if payload.get("type") != "access":
        raise AppError("UNAUTHORIZED", "Access token required", 401)

    # [SECURITE] JTI deny-list check — rejects revoked access tokens immediately.
    jti = payload.get("jti")
    redis = getattr(request.app.state, "arq_pool", None)
    if redis and jti and await is_jti_revoked(redis, jti):
        raise AppError("UNAUTHORIZED", "Token has been revoked", 401)

    # [SECURITE] User-disabled flag — rejects tokens belonging to disabled accounts.
    user_id_str = payload.get("sub")
    tenant_slug = payload.get("tenant_slug")
    if redis and user_id_str and await is_user_disabled(redis, int(user_id_str), tenant_slug):
        raise AppError("UNAUTHORIZED", "Account is disabled", 401)

    # [SECURITE] Revalide que le sub appartient bien au tenant reclame par le JWT --
    # le payload seul ne suffit pas pour choisir le schema Postgres (voir
    # user_belongs_to_tenant). Sans ce controle, un token dont le sub et le
    # tenant_slug sont incoherents resoudrait vers l'utilisateur reel portant
    # cet id dans le tenant reclame (les ids repartent a 1 par schema).
    if user_id_str and tenant_slug:
        from app.core.tenancy.tenant import user_belongs_to_tenant
        if not await user_belongs_to_tenant(int(user_id_str), tenant_slug, payload.get("email")):
            raise AppError("UNAUTHORIZED", "Invalid token", 401)
    # [SECURITE] Meme controle pour le flux super-admin independant (pas de
    # tenant_slug -- voir app.modules.super_admin.router.super_admin_login) :
    # revalide que le sub correspond a un compte reel et actif de
    # public.super_admins, plutot que de faire confiance au seul claim role.
    elif user_id_str and payload.get("role") == "super-admin":
        from app.core.auth.super_admin import super_admin_exists
        if not await super_admin_exists(int(user_id_str), payload.get("email")):
            raise AppError("UNAUTHORIZED", "Invalid token", 401)

    raw_user_id = payload.get("sub")
    user = {
        "id": int(raw_user_id) if raw_user_id is not None and str(raw_user_id).isdigit() else raw_user_id,
        "tenant_id": payload.get("tenant_id"),
        "tenant_slug": tenant_slug,
        "role": payload.get("role"),
        "permissions": payload.get("permissions"),
        "email": payload.get("email"),
        "must_change_password": payload.get("must_change_password", False),
        "jti": jti,
        "exp": payload.get("exp"),
    }
    request.state.user_id = user["id"]
    request.state.tenant_id = user["tenant_id"]
    request.state.tenant_slug = user["tenant_slug"]
    request.state.role = user["role"]

    # [SECURITE] Blocage si must_change_password=True — seuls /auth/change-password
    # et /auth/logout sont accessibles jusqu'à ce que le mot de passe soit changé.
    if user.get("must_change_password"):
        path = request.url.path
        if not any(allowed in path for allowed in _MUST_CHANGE_PASSWORD_ALLOWED_PATHS):
            raise AppError(
                "MUST_CHANGE_PASSWORD",
                "Vous devez changer votre mot de passe avant de continuer.",
                403,
            )

    # [SECURITE] Blocage si le tenant est suspendu — super-admin exempt.
    if user.get("role") != "super-admin" and user.get("tenant_slug"):
        from app.core.database import get_public_session
        from sqlalchemy import text as _text
        async with get_public_session() as pub_session:
            row = await pub_session.execute(
                _text(
                    "SELECT is_suspended, suspension_message "
                    "FROM public.tenants WHERE slug = :slug"
                ),
                {"slug": user["tenant_slug"]},
            )
            tenant_row = row.fetchone()
            if tenant_row and tenant_row.is_suspended:
                raise AppError(
                    "FORBIDDEN",
                    tenant_row.suspension_message or "Tenant suspendu",
                    403,
                )

    return user


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict | None:
    """Retourne l'utilisateur courant si authentifié, None sinon.

    Contrairement à ``get_current_user()``, ne lève pas 401 si le header
    ``Authorization`` est absent — permettant les endpoints mixtes (authentifié
    ou invité). Si un token est fourni mais invalide, 401 est quand même levé
    pour ne pas silencieusement traiter une tentative d'auth comme une commande invité.

    Args:
        request: Requête FastAPI courante.
        credentials: Credentials HTTP Bearer (optionnel).

    Returns:
        Dictionnaire utilisateur, ou ``None`` si aucun token fourni.

    Raises:
        AppError: UNAUTHORIZED (401) si un token est fourni mais invalide.
    """
    if credentials is None:
        return None
    return await get_current_user(request, credentials)


def require_role(*roles: str) -> Callable:
    """Fabrique une dependance FastAPI qui verifie le role de l'utilisateur courant.

    Args:
        *roles: Roles autorises. L'utilisateur doit posseder l'un d'eux.

    Returns:
        Dependance FastAPI async qui retourne le dict utilisateur si autorise,
        ou leve ``AppError`` FORBIDDEN (403) sinon.
    """
    async def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in roles:
            raise AppError("FORBIDDEN", "Insufficient permissions", 403)
        return current_user

    return dependency


def has_permission(current_user: dict, permission: str) -> bool:
    """Return True when a user has a fine-grained staff permission.

    Admin and super-admin keep full access. Staff accounts with permissions=None
    are treated as legacy unrestricted staff during rollout; once an admin stores
    an explicit list, it becomes authoritative.
    """
    role = current_user.get("role")
    if role in {"admin", "super-admin"}:
        return True
    permissions = current_user.get("permissions")
    if permissions is None:
        return role == "staff"
    return "*" in permissions or permission in permissions


def require_permission(permission: str, *roles: str) -> Callable:
    allowed_roles = roles or ("staff", "admin")

    async def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in allowed_roles and current_user.get("role") not in {"admin", "super-admin"}:
            raise AppError("FORBIDDEN", "Insufficient permissions", 403)
        if not has_permission(current_user, permission):
            raise AppError("FORBIDDEN", f"Missing permission: {permission}", 403)
        return current_user

    return dependency


def get_pagination(
    page: int = Query(1, ge=1, description="Numero de page (1-indexe)"),
    page_size: int = Query(20, ge=1, le=100, description="Elements par page"),
) -> PaginationParams:
    """Dependance FastAPI qui extrait et valide les parametres de pagination.

    Args:
        page: Numero de page depuis le query param ``page``.
        page_size: Taille de page depuis le query param ``page_size``.

    Returns:
        Instance ``PaginationParams`` validee.
    """
    return PaginationParams(page=page, page_size=page_size)


def get_arq_pool(request: Request) -> ArqRedis:
    """Retourne le pool arq partage depuis le singleton lifespan.

    Le pool est initialise une seule fois dans le lifespan FastAPI et stocke
    dans ``app.state.arq_pool``. Cette dependance evite la creation d'un nouveau
    pool Redis a chaque requete.

    Args:
        request: Requete FastAPI courante, utilisee pour acceder a ``app.state``.

    Returns:
        Instance ``ArqRedis`` prete a l'emploi pour ``enqueue_job``.
    """
    return request.app.state.arq_pool


def get_client_ip(request: Request) -> str | None:
    """Retourne l'IP réelle du client depuis ``request.client.host``.

    [🔒 SÉCURITÉ] Avec ``--proxy-headers`` sur uvicorn (Railway), le middleware
    ProxyHeadersMiddleware lit ``X-Forwarded-For`` et met à jour ``scope["client"]``
    avant que la requete n'atteigne FastAPI. Ce code ne lit donc JAMAIS directement
    l'en-tete ``X-Forwarded-For`` -- qui peut etre forgé par n'importe quel client.

    Ne pas appeler ce helper sans que ``--proxy-headers`` soit configure sur uvicorn
    en production, sinon ``client.host`` sera l'IP du proxy Railway (10.x.x.x).

    Args:
        request: Requete FastAPI courante.

    Returns:
        Adresse IP du client (string), ou ``None`` si indisponible.
    """
    return request.client.host if request.client else None


def get_client_ip_ws(websocket: WebSocket) -> str:
    """Retourne l'IP réelle d'une connexion WebSocket.

    [🔒 SÉCURITÉ] Identique a ``get_client_ip`` : repose sur ``--proxy-headers``
    uvicorn pour que ``client.host`` soit deja l'IP réelle.

    Args:
        websocket: Connexion WebSocket FastAPI/Starlette.

    Returns:
        Adresse IP du client, ou "unknown" si indisponible.
    """
    return websocket.client.host if websocket.client else "unknown"
