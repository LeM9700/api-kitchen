import jwt
from jwt.exceptions import PyJWTError as JWTError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import engine, tenant_schema_name
from app.core.http.errors import AppError

# Code SQLSTATE PostgreSQL pour "duplicate_schema" (un CREATE SCHEMA cible un nom
# deja pris). Comparer sur ce code plutot que sur le type Python de l'exception :
# le dialecte asyncpg de SQLAlchemy enveloppe l'exception asyncpg d'origine dans sa
# propre classe de compatibilite DBAPI (``exc.orig`` n'est donc PAS une instance de
# ``asyncpg.exceptions.DuplicateSchemaError``, seulement son ``__cause__`` non
# documente) -- le SQLSTATE, lui, est un identifiant stable du protocole PostgreSQL,
# expose de la meme facon quel que soit le driver.
POSTGRES_DUPLICATE_SCHEMA_SQLSTATE = "42P06"

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


async def _execute_create_schema(executor, schema: str) -> None:
    """Emet le ``CREATE SCHEMA`` et traduit une collision en ``AppError``.

    ``executor`` est soit une ``AsyncConnection`` soit une ``AsyncSession`` --
    les deux exposent une methode ``execute`` compatible, ce qui permet
    d'utiliser cette fonction aussi bien en transaction dediee
    (``create_tenant_schema``) qu'inseree dans la transaction d'un appelant
    (``create_tenant_schema_on``).

    [SECURITE] Pas de ``IF NOT EXISTS`` : un schema deja present sous ce nom
    (collision de slugs, schema residuel d'un tenant precedent, creation
    concurrente) doit faire echouer la creation plutot que d'etre reutilise en
    silence -- un ``CREATE SCHEMA IF NOT EXISTS`` masquerait le fait qu'un
    tenant s'appreterait a partager le schema, donc les donnees, d'un autre
    tenant. PostgreSQL rejette lui-meme la creation d'un schema deja existant
    (``DuplicateSchemaError``, SQLSTATE 42P06) : on s'appuie sur cette
    contrainte atomique au niveau base plutot que sur un ``SELECT`` prealable,
    qui laisserait une fenetre de course entre la verification et la creation.

    Raises:
        AppError: TENANT_SCHEMA_COLLISION (409) si un schema du meme nom existe deja.
    """
    try:
        await executor.execute(text(f'CREATE SCHEMA "{schema}"'))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == POSTGRES_DUPLICATE_SCHEMA_SQLSTATE:
            raise AppError(
                "TENANT_SCHEMA_COLLISION",
                "A PostgreSQL schema already exists for this tenant identifier",
                409,
                "tenant_slug",
            ) from exc
        raise


async def create_tenant_schema(tenant_slug: str) -> None:
    """Cree le schema PostgreSQL dedie a un tenant, dans sa propre transaction.

    A n'utiliser que lorsque la creation du schema n'a pas besoin d'etre
    atomique avec une autre ecriture (ex. un script d'admin ponctuel). Le
    parcours normal de creation de tenant doit utiliser
    ``create_tenant_schema_on`` (voir sa docstring) pour eviter qu'une
    collision laisse une ligne ``public.tenants`` orpheline.

    Raises:
        AppError: TENANT_SCHEMA_COLLISION (409) si un schema du meme nom existe deja.
    """
    schema = tenant_schema_name(tenant_slug)
    async with engine.begin() as conn:
        await _execute_create_schema(conn, schema)


async def create_tenant_schema_on(executor, tenant_slug: str) -> None:
    """Cree le schema PostgreSQL dedie a un tenant SUR LA TRANSACTION DE L'APPELANT.

    [SECURITE] A utiliser dans le meme bloc transactionnel que l'insertion de
    la ligne ``public.tenants`` correspondante, AVANT le ``commit`` de
    l'appelant. Si le schema existe deja (collision de troncature, residu,
    creation concurrente), ``AppError`` remonte et l'appelant n'a JAMAIS
    besoin de compenser explicitement : le rollback implicite de la
    transaction (session fermee sans commit, ou exception propagee hors du
    ``async with`` du connection) annule aussi l'insertion -- aucune ligne
    orpheline dans ``public.tenants`` n'est possible.

    Args:
        executor: ``AsyncConnection`` ou ``AsyncSession`` ouverte par l'appelant,
            non encore commitee.
        tenant_slug: Slug deja valide (voir ``NEW_TENANT_SLUG_RE``).

    Raises:
        AppError: TENANT_SCHEMA_COLLISION (409) si un schema du meme nom existe deja.
    """
    schema = tenant_schema_name(tenant_slug)
    await _execute_create_schema(executor, schema)


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

    Conserve pour compatibilite (utilise par le handler WebSocket et les
    tests existants) -- pour le flux HTTP, ``get_current_user`` utilise
    desormais ``get_live_tenant_user_state`` qui fait la meme verification
    tout en renvoyant l'etat courant (role/permissions/is_active).
    """
    state = await get_live_tenant_user_state(user_id, tenant_slug)
    return state is not None and email is not None and state.email == email


class LiveTenantUserState:
    """Etat courant (base) d'un utilisateur tenant, relu a chaque requete.

    [🔒 SÉCURITÉ] Sert a ne JAMAIS faire confiance aux claims ``role`` /
    ``permissions`` embarques dans un access token deja emis : un retrait de
    permission ou une desactivation doit prendre effet immediatement, pas
    seulement a l'expiration du token (voir ``app.core.http.deps.get_current_user``,
    qui ecrase les claims du JWT par cet etat frais a chaque requete). C'est
    la solution retenue ici en equivalent d'une "version de session" ou d'une
    liste de revocation : comme ce module fait deja un aller-retour base par
    requete pour valider l'appartenance au tenant, autant y lire l'etat
    autoritaire plutot que de faire confiance a une copie figee dans le JWT.
    """

    __slots__ = ("id", "email", "role", "permissions", "is_active")

    def __init__(self, id: int, email: str, role: str, permissions: list[str] | None, is_active: bool):
        self.id = id
        self.email = email
        self.role = role
        self.permissions = permissions
        self.is_active = is_active


async def get_live_tenant_user_state(user_id: int, tenant_slug: str) -> LiveTenantUserState | None:
    """Relit l'etat courant (role/permissions/is_active/email) d'un utilisateur tenant.

    Args:
        user_id: ``sub`` du JWT.
        tenant_slug: Slug du tenant reclame par le JWT.

    Returns:
        ``LiveTenantUserState`` si l'utilisateur existe dans ce schema tenant,
        ``None`` sinon (schema inexistant, id inconnu...).
    """
    from app.core.database import get_tenant_session
    from app.modules.auth.models import User

    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        return LiveTenantUserState(
            id=user.id,
            email=user.email,
            role=user.role,
            permissions=user.permissions,
            is_active=user.is_active,
        )
