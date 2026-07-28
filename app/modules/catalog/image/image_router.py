"""Router FastAPI pour la gestion des images de catalogue (galerie multi-entités)."""


from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.database import get_tenant_session
from app.core.http.deps import require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.catalog.image import image_service
from app.modules.catalog.image.image_schemas import ImageReorderBody, MediaImageOut

router = APIRouter()

# Valeurs autorisées pour le paramètre de chemin entity_type
_VALID_ENTITY_TYPES = frozenset({"products", "categories", "extras", "variants"})

_IMAGE_REQUIREMENTS_HEADER = "Formats: JPG, PNG, WebP, SVG | Max: 8 Mo | Recommandé: 1200x1200px minimum"


def _validate_entity_type(entity_type: str) -> str:
    """Valide le paramètre entity_type et lève une AppError 422 si invalide.

    Args:
        entity_type: Valeur brute du path parameter.

    Returns:
        La valeur validée.

    Raises:
        AppError: ``INVALID_ENTITY_TYPE`` (422) si la valeur n'est pas autorisée.
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise AppError(
            "INVALID_ENTITY_TYPE",
            f"Type d'entité invalide : '{entity_type}'. Valeurs autorisées : {sorted(_VALID_ENTITY_TYPES)}.",
            status_code=422,
        )
    return entity_type


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.post(
    "/{entity_type}/{entity_id}/images",
    response_model=MediaImageOut,
    status_code=201,
    summary="Upload une image pour une entité du catalogue",
)
@limiter.limit("20/minute")
async def upload_image(
    request: Request,
    entity_type: str,
    entity_id: int,
    file: UploadFile,
    alt_text: str | None = None,
    is_primary: bool = False,
    current_user: dict = Depends(require_role("staff", "admin")),
) -> JSONResponse:
    """Upload et associe une image à une entité du catalogue.

    Args:
        entity_type: Type d'entité (``products``, ``categories``, ``extras``, ``variants``).
        entity_id: Identifiant de l'entité cible.
        file: Fichier image multipart (JPG, PNG, WebP, SVG — max 8 Mo).
        alt_text: Texte alternatif optionnel pour l'accessibilité.
        is_primary: Si True, cette image devient la principale de l'entité.
        current_user: Utilisateur authentifié (role staff ou admin requis).

    Returns:
        ``MediaImageOut`` avec les URLs générées.
    """
    _validate_entity_type(entity_type)

    file_bytes = await file.read()
    filename = file.filename or "upload"
    tenant_slug: str = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        image = await image_service.upload_entity_image(
            session=session,
            tenant_slug=tenant_slug,
            entity_type=entity_type,
            entity_id=entity_id,
            file_bytes=file_bytes,
            filename=filename,
            alt_text=alt_text,
            is_primary=is_primary,
        )

    response_data = MediaImageOut.model_validate(image)
    return JSONResponse(
        content=response_data.model_dump(mode="json"),
        status_code=201,
        headers={"X-Image-Requirements": _IMAGE_REQUIREMENTS_HEADER},
    )


# ---------------------------------------------------------------------------
# Liste (publique — lecture seule)
# ---------------------------------------------------------------------------

@router.get(
    "/{entity_type}/{entity_id}/images",
    response_model=list[MediaImageOut],
    summary="Liste les images d'une entité du catalogue",
)
async def list_images(
    request: Request,
    entity_type: str,
    entity_id: int,
) -> list[MediaImageOut]:
    """Retourne la galerie d'images d'une entité, triée par ``display_order``.

    Route publique (pas d'authentification requise).

    Args:
        entity_type: Type d'entité (``products``, ``categories``, ``extras``, ``variants``).
        entity_id: Identifiant de l'entité.

    Returns:
        Liste de ``MediaImageOut`` ordonnée.
    """
    _validate_entity_type(entity_type)
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")

    async with get_tenant_session(tenant_slug) as session:
        images = await image_service.list_entity_images(session, entity_type, entity_id)

    return [MediaImageOut.model_validate(img) for img in images]


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

@router.delete(
    "/images/{image_id}",
    status_code=204,
    summary="Supprime une image (Cloudinary + base de données)",
)
async def delete_image(
    image_id: int,
    current_user: dict = Depends(require_role("staff", "admin")),
) -> None:
    """Supprime une image de Cloudinary et de la base de données.

    Si l'image supprimée était la principale, la suivante est auto-promue.

    Args:
        image_id: Clé primaire de l'image.
        current_user: Utilisateur authentifié (role staff ou admin requis).
    """
    tenant_slug: str = current_user["tenant_slug"]
    async with get_tenant_session(tenant_slug) as session:
        await image_service.delete_entity_image(session, image_id, tenant_slug)


# ---------------------------------------------------------------------------
# Définir comme principale
# ---------------------------------------------------------------------------

@router.patch(
    "/images/{image_id}/primary",
    response_model=MediaImageOut,
    summary="Définit une image comme principale pour son entité",
)
async def set_primary(
    image_id: int,
    entity_type: str,
    entity_id: int,
    current_user: dict = Depends(require_role("staff", "admin")),
) -> MediaImageOut:
    """Définit l'image spécifiée comme principale de son entité.

    Retire le flag ``is_primary`` de toutes les autres images de l'entité.

    Args:
        image_id: Clé primaire de l'image à promouvoir.
        entity_type: Type d'entité (query param, ex: ``products``).
        entity_id: Identifiant de l'entité (query param).
        current_user: Utilisateur authentifié (role staff ou admin requis).

    Returns:
        ``MediaImageOut`` mis à jour.
    """
    _validate_entity_type(entity_type)
    tenant_slug: str = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        image = await image_service.set_primary_image(session, image_id, entity_type, entity_id)

    return MediaImageOut.model_validate(image)


# ---------------------------------------------------------------------------
# Réordonnancement
# ---------------------------------------------------------------------------

@router.patch(
    "/{entity_type}/{entity_id}/images/reorder",
    response_model=list[MediaImageOut],
    summary="Réordonne la galerie d'images d'une entité",
)
async def reorder_images(
    entity_type: str,
    entity_id: int,
    body: ImageReorderBody,
    current_user: dict = Depends(require_role("staff", "admin")),
) -> list[MediaImageOut]:
    """Réordonne les images d'une entité selon la liste d'IDs fournie.

    Args:
        entity_type: Type d'entité (``products``, ``categories``, ``extras``, ``variants``).
        entity_id: Identifiant de l'entité.
        body: ``ImageReorderBody`` avec ``image_ids`` dans l'ordre souhaité.
        current_user: Utilisateur authentifié (role staff ou admin requis).

    Returns:
        Liste de ``MediaImageOut`` dans le nouvel ordre.
    """
    _validate_entity_type(entity_type)
    tenant_slug: str = current_user["tenant_slug"]

    async with get_tenant_session(tenant_slug) as session:
        images = await image_service.reorder_images(
            session, body.image_ids, entity_type, entity_id
        )

    return [MediaImageOut.model_validate(img) for img in images]
