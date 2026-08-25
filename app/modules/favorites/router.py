"""Endpoints REST pour la gestion des favoris produits.

Routes exposées :
- ``POST   /favorites``             — Marque un produit favori (idempotent).
- ``DELETE /favorites/{product_id}`` — Retire un produit des favoris.
- ``GET    /favorites``              — Liste les favoris de l'utilisateur courant.

Toutes les routes nécessitent un utilisateur authentifié (``get_current_user``).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user
from app.modules.favorites.models import Favorite
from app.modules.favorites.schemas import FavoriteCreate, FavoriteResponse

router = APIRouter()


@router.post(
    "",
    response_model=FavoriteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Marquer un produit favori",
)
async def add_favorite(
    body: FavoriteCreate,
    current_user: dict = Depends(get_current_user),
) -> FavoriteResponse:
    """Ajoute un produit aux favoris de l'utilisateur courant.

    Idempotent : si le produit est déjà favori, renvoie l'enregistrement
    existant plutôt que de lever une erreur de contrainte unique — un
    "toggle" côté client peut appeler cette route sans avoir à connaître
    l'état exact côté serveur.

    Args:
        body: Payload validé par Pydantic (product_id).
        current_user: Utilisateur extrait du JWT.

    Returns:
        FavoriteResponse de l'enregistrement créé ou déjà existant.
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        existing = await session.scalar(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.product_id == body.product_id,
            )
        )
        if existing:
            return FavoriteResponse.model_validate(existing)

        favorite = Favorite(user_id=user_id, product_id=body.product_id)
        session.add(favorite)
        await session.commit()
        await session.refresh(favorite)
        return FavoriteResponse.model_validate(favorite)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un produit des favoris",
)
async def remove_favorite(
    product_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Retire un produit des favoris de l'utilisateur courant.

    Idempotent : ne lève pas d'erreur si le produit n'était pas favori
    (même raisonnement que [add_favorite] — un "toggle" côté client ne
    connaît pas toujours l'état exact côté serveur).

    Args:
        product_id: Identifiant du produit à retirer.
        current_user: Utilisateur extrait du JWT.
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        existing = await session.scalar(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.product_id == product_id,
            )
        )
        if existing:
            await session.delete(existing)
            await session.commit()


@router.get(
    "",
    response_model=list[FavoriteResponse],
    summary="Lister mes favoris",
)
async def list_favorites(
    current_user: dict = Depends(get_current_user),
) -> list[FavoriteResponse]:
    """Retourne tous les produits favoris de l'utilisateur courant.

    Args:
        current_user: Utilisateur extrait du JWT.

    Returns:
        Liste des FavoriteResponse, du plus récent au plus ancien.
    """
    user_id = int(current_user["id"])
    tenant_slug = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        result = await session.execute(
            select(Favorite)
            .where(Favorite.user_id == user_id)
            .order_by(Favorite.created_at.desc())
        )
        favorites = list(result.scalars())

    return [FavoriteResponse.model_validate(f) for f in favorites]
