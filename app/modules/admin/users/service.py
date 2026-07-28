"""Service métier pour la gestion des utilisateurs tenant par les admins."""

import secrets
from datetime import datetime, timezone

from sqlalchemy import func, update
from sqlalchemy.future import select

from app.core.database import get_tenant_session
from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.core.auth.security import get_password_hash
from app.modules.auth.models import RefreshToken, User


async def list_users(
    tenant_slug: str,
    pagination: PaginationParams,
    role: str | None = None,
    is_active: bool | None = None,
    email_verified: bool | None = None,
) -> tuple[list[dict], int]:
    """Retourne la liste paginée des utilisateurs du tenant avec filtres optionnels.

    Args:
        tenant_slug: Slug du tenant dont on liste les utilisateurs.
        pagination: Paramètres de pagination (page, page_size).
        role: Filtre par rôle ("staff", "admin", "customer"…).
        is_active: Filtre par statut actif/inactif.
        email_verified: Filtre par vérification d'email.

    Returns:
        Tuple (liste de dicts AdminUserOut, total d'enregistrements toutes pages).
    """
    async with get_tenant_session(tenant_slug) as session:

        def _apply_filters(q):
            if role is not None:
                q = q.where(User.role == role)
            if is_active is not None:
                q = q.where(User.is_active.is_(is_active))
            if email_verified is not None:
                if email_verified:
                    q = q.where(User.email_verified_at.isnot(None))
                else:
                    q = q.where(User.email_verified_at.is_(None))
            return q

        total: int = int(
            await session.scalar(_apply_filters(select(func.count(User.id)))) or 0
        )

        offset = (pagination.page - 1) * pagination.page_size
        stmt = _apply_filters(select(User)).offset(offset).limit(pagination.page_size)
        result = await session.execute(stmt)
        users = result.scalars().all()

        items = [
            {
                "id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "permissions": u.permissions,
                "is_active": u.is_active,
                "email_verified": u.email_verified_at is not None,
                "created_at": u.created_at,
                "must_change_password": u.must_change_password,
            }
            for u in users
        ]
        return items, total


async def create_user(tenant_slug: str, body) -> dict:
    """Crée un utilisateur avec un mot de passe temporaire et le marque pour changement.

    L'email est marqué comme vérifié immédiatement (comptes créés par admin
    ne nécessitent pas de vérification email).

    Args:
        tenant_slug: Slug du tenant dans lequel créer l'utilisateur.
        body: AdminUserCreate validé (email, full_name, role).

    Returns:
        Dictionnaire avec id, email, role et temporary_password.

    Raises:
        AppError: EMAIL_EXISTS (409) si l'email est déjà enregistré.
    """
    temp_password = secrets.token_urlsafe(12)  # produit ~16 chars URL-safe
    async with get_tenant_session(tenant_slug) as session:
        existing = await session.scalar(select(User).where(User.email == body.email))
        if existing:
            raise AppError("EMAIL_EXISTS", "Email already registered", 409, "email")

        user = User(
            email=body.email,
            full_name=body.full_name,
            password_hash=get_password_hash(temp_password),
            role=body.role,
            permissions=body.permissions,
            must_change_password=True,
            # [SECURITE] Admin-created accounts skip email verification.
            email_verified_at=datetime.now(timezone.utc),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "temporary_password": temp_password,
    }


async def update_user_permissions(user_id: int, tenant_slug: str, permissions: list[str]) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("NOT_FOUND", "User not found", 404)
        if user.role not in {"staff", "admin"}:
            raise AppError("INVALID_ROLE", "Permissions can only be assigned to staff/admin users", 422, "role")
        user.permissions = permissions
        await session.commit()
        await session.refresh(user)
        return {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "permissions": user.permissions,
            "is_active": user.is_active,
            "email_verified": user.email_verified_at is not None,
            "created_at": user.created_at,
            "must_change_password": user.must_change_password,
        }


async def deactivate_user(user_id: int, tenant_slug: str, redis=None) -> None:
    """Désactive un utilisateur : is_active=False, révoque tous ses refresh tokens.

    Si redis est fourni, pose le flag user_disabled pour invalider immédiatement
    les access tokens encore valides en circulation.

    Args:
        user_id: Identifiant de l'utilisateur à désactiver.
        tenant_slug: Slug du tenant.
        redis: ArqRedis instance (optionnel, depuis app.state.arq_pool).

    Raises:
        AppError: NOT_FOUND (404) si l'utilisateur n'existe pas.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("NOT_FOUND", "User not found", 404)
        user.is_active = False
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()

    if redis is not None:
        from app.core.auth.token_revocation import flag_user_disabled
        await flag_user_disabled(redis, user_id, tenant_slug)


async def reactivate_user(user_id: int, tenant_slug: str, redis=None) -> None:
    """Réactive un utilisateur précédemment désactivé.

    Supprime le flag user_disabled dans Redis pour que les nouvelles
    authentifications soient à nouveau acceptées.

    Args:
        user_id: Identifiant de l'utilisateur à réactiver.
        tenant_slug: Slug du tenant.
        redis: ArqRedis instance (optionnel).

    Raises:
        AppError: NOT_FOUND (404) si l'utilisateur n'existe pas.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("NOT_FOUND", "User not found", 404)
        user.is_active = True
        await session.commit()

    if redis is not None:
        from app.core.auth.token_revocation import clear_user_disabled
        await clear_user_disabled(redis, user_id, tenant_slug)


async def admin_reset_password(user_id: int, tenant_slug: str, redis=None) -> dict:
    """Réinitialise le mot de passe d'un utilisateur avec un mot de passe temporaire.

    Révoque tous les refresh tokens et pose le flag user_disabled pour forcer
    une nouvelle authentification avec le mot de passe temporaire.

    Args:
        user_id: Identifiant de l'utilisateur.
        tenant_slug: Slug du tenant.
        redis: ArqRedis instance (optionnel).

    Returns:
        Dictionnaire {"temporary_password": str}.

    Raises:
        AppError: NOT_FOUND (404) si l'utilisateur n'existe pas.
    """
    temp_password = secrets.token_urlsafe(12)  # ~16 chars
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("NOT_FOUND", "User not found", 404)
        user.password_hash = get_password_hash(temp_password)
        user.must_change_password = True
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()

    if redis is not None:
        from app.core.auth.token_revocation import flag_user_disabled
        await flag_user_disabled(redis, user_id, tenant_slug)

    return {"temporary_password": temp_password}
