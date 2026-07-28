"""Router FastAPI pour la gestion des utilisateurs par les admins tenant."""

from fastapi import APIRouter, Depends, Query, Request

from app.core.http.deps import get_pagination, require_role
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.admin.users import service as users_service
from app.modules.admin.users.schemas import (
    AdminUserCreate,
    AdminUserCreateResponse,
    AdminUserOut,
    AdminUserPermissionsUpdate,
)

router = APIRouter()


@router.get("", response_model=PaginatedResponse[AdminUserOut])
async def list_users(
    request: Request,
    role: str | None = Query(None, description="Filtre par rôle"),
    is_active: bool | None = Query(None, description="Filtre par statut actif"),
    email_verified: bool | None = Query(None, description="Filtre par vérification email"),
    pagination: PaginationParams = Depends(get_pagination),
    current_user: dict = Depends(require_role("admin")),
) -> PaginatedResponse[AdminUserOut]:
    """Liste les utilisateurs du tenant avec filtres, pagination et total.

    Args:
        request: Requête FastAPI.
        role: Filtre optionnel par rôle.
        is_active: Filtre optionnel par statut actif.
        email_verified: Filtre optionnel par vérification d'email.
        pagination: Paramètres de pagination (page, page_size).
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        PaginatedResponse[AdminUserOut] avec items, total, page, page_size, pages.
    """
    items, total = await users_service.list_users(
        current_user["tenant_slug"],
        pagination,
        role=role,
        is_active=is_active,
        email_verified=email_verified,
    )
    return PaginatedResponse.build(items, total, pagination)


@router.post("", response_model=AdminUserCreateResponse, status_code=201)
async def create_user(
    body: AdminUserCreate,
    current_user: dict = Depends(require_role("admin")),
) -> AdminUserCreateResponse:
    """Crée un nouvel utilisateur staff/admin avec un mot de passe temporaire.

    Args:
        body: Données du nouvel utilisateur (email, full_name, role).
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        AdminUserCreateResponse avec le mot de passe temporaire.
    """
    return await users_service.create_user(current_user["tenant_slug"], body)


@router.patch("/{user_id}/permissions", response_model=AdminUserOut)
async def update_user_permissions(
    user_id: int,
    body: AdminUserPermissionsUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> AdminUserOut:
    return await users_service.update_user_permissions(
        user_id,
        current_user["tenant_slug"],
        body.permissions,
    )


@router.patch("/{user_id}/deactivate", status_code=200)
async def deactivate_user(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
) -> dict:
    """Désactive un utilisateur et révoque tous ses tokens actifs.

    Args:
        user_id: Identifiant de l'utilisateur à désactiver.
        request: Requête FastAPI (pour accéder à app.state.arq_pool).
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        Message de confirmation.
    """
    redis = getattr(request.app.state, "arq_pool", None)
    await users_service.deactivate_user(user_id, current_user["tenant_slug"], redis=redis)
    return {"message": "User deactivated"}


@router.patch("/{user_id}/reactivate", status_code=200)
async def reactivate_user(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
) -> dict:
    """Réactive un utilisateur précédemment désactivé.

    Args:
        user_id: Identifiant de l'utilisateur à réactiver.
        request: Requête FastAPI.
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        Message de confirmation.
    """
    redis = getattr(request.app.state, "arq_pool", None)
    await users_service.reactivate_user(user_id, current_user["tenant_slug"], redis=redis)
    return {"message": "User reactivated"}


@router.post("/{user_id}/reset-password", status_code=200)
async def admin_reset_password(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
) -> dict:
    """Réinitialise le mot de passe d'un utilisateur avec un mot de passe temporaire.

    Args:
        user_id: Identifiant de l'utilisateur.
        request: Requête FastAPI.
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        Dictionnaire {"temporary_password": str}.
    """
    redis = getattr(request.app.state, "arq_pool", None)
    return await users_service.admin_reset_password(
        user_id, current_user["tenant_slug"], redis=redis
    )
