"""Customer self-service operations: registration, profile management, account deletion."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.future import select

from app.core.auth.security import get_password_hash, verify_password
from app.core.auth.token_revocation import flag_user_disabled
from app.core.database import get_public_session, get_tenant_session
from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.auth.models import RefreshToken, User
from app.modules.auth.service import issue_tokens
from app.modules.customer.schemas import (
    CustomerDataExportOut,
    CustomerOrderExportOut,
    CustomerOut,
    CustomerRegisterRequest,
    CustomerUpdateRequest,
)

# [RGPD] Nombre max de commandes incluses dans un export -- couvre la grande
# majorite des clients sans risquer une reponse non bornee. `orders_truncated`
# signale explicitement si des commandes plus anciennes ont ete omises.
_EXPORT_MAX_ORDERS = 100

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_customer_out(user: User) -> CustomerOut:
    """Construit un CustomerOut a partir d'une instance User.

    Args:
        user: Instance SQLAlchemy User chargee depuis la base tenant.

    Returns:
        Schema Pydantic CustomerOut serialisable.
    """
    return CustomerOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        role=user.role,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def register(
    tenant_slug: str,
    body: CustomerRegisterRequest,
    arq_pool=None,
) -> tuple[User, str, str, int]:
    """Inscrit un nouveau client sur le tenant identifie par tenant_slug.

    Cree le compte avec le role 'customer', genere un token de verification email
    (UUID4, expiry 24h) et emet une paire access/refresh token.

    Args:
        tenant_slug: Slug du tenant cible (doit exister en base publique).
        body: Payload valide CustomerRegisterRequest.
        arq_pool: Pool arq optionnel pour l'envoi de l'email de verification.

    Returns:
        Tuple (user, access_token, refresh_token, session_id).

    Raises:
        AppError: TENANT_NOT_FOUND (404) si le slug est inconnu.
        AppError: EMAIL_ALREADY_EXISTS (409) si l'email est deja enregistre.
    """
    # 1. Verifier que le tenant existe en base publique.
    async with get_public_session() as pub:
        from sqlalchemy import text

        result = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        tenant_id = result.scalar_one_or_none()

    if tenant_id is None:
        raise AppError("TENANT_NOT_FOUND", "Tenant not found", 404, "tenant_slug")

    # 2. Hash password first (timing-safe: always run bcrypt regardless of email existence)
    password_hash = get_password_hash(body.password)

    # 3. Verifier l'unicite de l'email dans le schema tenant.
    async with get_tenant_session(tenant_slug) as session:
        existing = await session.scalar(select(User).where(User.email == body.email))
        if existing is not None:
            raise AppError("EMAIL_ALREADY_EXISTS", "Email already registered", 409, "email")

        # 4. Creer l'utilisateur.
        verification_token = str(uuid.uuid4())
        verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        user = User(
            email=body.email,
            password_hash=password_hash,  # use the pre-computed hash
            full_name=body.full_name,
            phone=body.phone,
            role="customer",
            email_verification_token=verification_token,
            email_verification_expires_at=verification_expires_at,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # 5. Emettre les tokens (flush interne, commit a notre charge).
        access, refresh, session_id = await issue_tokens(session, user, tenant_id, tenant_slug)
        await session.commit()

    # 6. Enqueuer l'email de verification de maniere non bloquante.
    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_verification_email",
                tenant_slug=tenant_slug,
                user_id=user.id,
                token=verification_token,
            )
        except Exception:
            pass  # Non critique : le user peut demander un renvoi

    return user, access, refresh, session_id


async def get_profile(user_id: int, tenant_slug: str) -> CustomerOut:
    """Retourne le profil du client identifie par user_id.

    Args:
        user_id: Identifiant de l'utilisateur dans le schema tenant.
        tenant_slug: Slug du tenant cible.

    Returns:
        Schema CustomerOut avec les informations de profil.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        return _build_customer_out(user)


async def export_my_data(user_id: int, tenant_slug: str) -> CustomerDataExportOut:
    """Exporte les donnees personnelles du client (profil + historique de commandes).

    [RGPD] Repond au droit a la portabilite des donnees (Art. 20 RGPD) pour le
    perimetre couvert par ce module -- profil + commandes. Voir PRIVACY.md pour
    le perimetre complet et les limites connues.

    Args:
        user_id: Identifiant de l'utilisateur dans le schema tenant.
        tenant_slug: Slug du tenant cible.

    Returns:
        CustomerDataExportOut avec le profil, les commandes recentes et un
        indicateur de troncature si l'historique depasse `_EXPORT_MAX_ORDERS`.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
    """
    from app.modules.orders.service import list_my_orders  # noqa: PLC0415 (evite import circulaire au chargement du module)

    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        profile = _build_customer_out(user)

        pagination = PaginationParams(page=1, page_size=_EXPORT_MAX_ORDERS)
        orders, total = await list_my_orders(session, pagination, user_id)

    return CustomerDataExportOut(
        profile=profile,
        orders=[CustomerOrderExportOut(**order) for order in orders],
        orders_truncated=total > _EXPORT_MAX_ORDERS,
        exported_at=datetime.now(timezone.utc),
    )


async def update_profile(
    user_id: int,
    tenant_slug: str,
    body: CustomerUpdateRequest,
) -> CustomerOut:
    """Met a jour les champs modifiables du profil client.

    Seuls les champs fournis (non-None) sont mis a jour.

    Args:
        user_id: Identifiant de l'utilisateur dans le schema tenant.
        tenant_slug: Slug du tenant cible.
        body: Payload CustomerUpdateRequest (champs optionnels).

    Returns:
        Schema CustomerOut mis a jour.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)

        if body.full_name is not None:
            user.full_name = body.full_name
        if body.phone is not None:
            user.phone = body.phone

        await session.commit()
        await session.refresh(user)
        return _build_customer_out(user)


async def delete_account(
    user_id: int,
    tenant_slug: str,
    password: str,
    redis=None,
) -> None:
    """Supprime logiquement le compte client apres verification du mot de passe.

    Revoque tous les refresh tokens actifs, desactive l'utilisateur et
    invalide son access token en Redis si le pool est disponible.

    Args:
        user_id: Identifiant de l'utilisateur dans le schema tenant.
        tenant_slug: Slug du tenant cible.
        password: Mot de passe en clair pour confirmation.
        redis: Client ArqRedis optionnel pour invalider l'access token.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
        AppError: INVALID_CREDENTIALS (401) si le mot de passe est incorrect.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)

        if not verify_password(password, user.password_hash):
            raise AppError("INVALID_CREDENTIALS", "Invalid password", 401)

        # Revoquer tous les refresh tokens actifs de l'utilisateur.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )

        user.is_active = False
        await session.commit()

    # Invalider l'access token en Redis de maniere non bloquante.
    # `redis` is the arq pool (ArqRedis), which exposes raw Redis commands (.set, .exists, etc.)
    # — same pattern as auth/service.py::reset_password. flag_user_disabled() uses .set(), which
    # is fully compatible with ArqRedis.
    if redis is not None:
        await flag_user_disabled(redis, user_id, tenant_slug)
