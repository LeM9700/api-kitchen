"""Service métier pour la gestion des images de catalogue (galerie multi-entités).

Architecture : image_router → image_service → cloudinary_service + SQLAlchemy.
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services import cloudinary as cloudinary_service
from app.core.http.errors import AppError
from app.modules.catalog.image.image_model import MediaImage
from app.modules.catalog.models import Category, Extra, Product, ProductVariant

# Mapping entity_type (slug URL) → modèle SQLAlchemy
_ENTITY_MODEL_MAP: dict = {
    "products": Product,
    "categories": Category,
    "extras": Extra,
    "variants": ProductVariant,
}

# Mapping entity_type URL → valeur stockée en base (singulier)
_ENTITY_TYPE_DB_MAP: dict[str, str] = {
    "products": "product",
    "categories": "category",
    "extras": "extra",
    "variants": "variant",
}

_ENTITY_TYPE_URL_MAP: dict[str, str] = {value: key for key, value in _ENTITY_TYPE_DB_MAP.items()}

MAX_IMAGES_PER_TENANT = 500
MAX_IMAGES_PER_ENTITY = 10


def _expected_cloudinary_prefix(tenant_slug: str, entity_type: str, entity_id: int) -> str:
    return f"pizza/{tenant_slug}/{entity_type}/{entity_id}/"


async def _assert_entity_exists(
    session: AsyncSession, entity_type: str, entity_id: int
) -> None:
    """Vérifie qu'une entité existe en base, lève AppError sinon.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        entity_type: Slug d'entité (``products``, ``categories``, ``extras``, ``variants``).
        entity_id: Identifiant de l'entité.

    Raises:
        AppError: ``ENTITY_NOT_FOUND`` (404) si l'entité est absente.
    """
    model = _ENTITY_MODEL_MAP[entity_type]
    obj = await session.get(model, entity_id)
    if obj is None:
        raise AppError(
            "ENTITY_NOT_FOUND",
            f"{entity_type[:-1].capitalize()} #{entity_id} introuvable.",
            status_code=404,
        )


async def upload_entity_image(
    session: AsyncSession,
    tenant_slug: str,
    entity_type: str,
    entity_id: int,
    file_bytes: bytes,
    filename: str,
    alt_text: str | None = None,
    is_primary: bool = False,
) -> MediaImage:
    """Upload une image et l'associe à une entité du catalogue.

    Si aucune image n'existe encore pour l'entité (première image uploadée),
    ``is_primary`` est automatiquement forcé à ``True`` indépendamment du paramètre.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        tenant_slug: Slug du tenant pour le dossier Cloudinary.
        entity_type: Slug d'entité URL (``products``, ``categories``, ...).
        entity_id: Identifiant de l'entité cible.
        file_bytes: Contenu brut du fichier.
        filename: Nom du fichier original (pour validation d'extension).
        alt_text: Texte alternatif optionnel.
        is_primary: Si True, cette image devient la principale de l'entité.

    Returns:
        Instance ``MediaImage`` persistée.

    Raises:
        AppError: ``ENTITY_NOT_FOUND`` si l'entité n'existe pas.
        AppError: Propagées depuis ``cloudinary_service.upload_image``.
    """
    await _assert_entity_exists(session, entity_type, entity_id)

    db_entity_type = _ENTITY_TYPE_DB_MAP[entity_type]

    tenant_image_count = await session.scalar(select(func.count()).select_from(MediaImage)) or 0
    if tenant_image_count >= MAX_IMAGES_PER_TENANT:
        raise AppError(
            "IMAGE_TENANT_QUOTA_EXCEEDED",
            f"Quota d'images tenant atteint ({MAX_IMAGES_PER_TENANT}).",
            status_code=429,
        )

    entity_image_count = await session.scalar(
        select(func.count())
        .select_from(MediaImage)
        .where(MediaImage.entity_type == db_entity_type, MediaImage.entity_id == entity_id)
    ) or 0
    if entity_image_count >= MAX_IMAGES_PER_ENTITY:
        raise AppError(
            "IMAGE_ENTITY_QUOTA_EXCEEDED",
            f"Quota d'images pour cette entite atteint ({MAX_IMAGES_PER_ENTITY}).",
            status_code=429,
        )

    # Retirer le flag primary sur les images existantes si nécessaire
    if is_primary:
        await session.execute(
            update(MediaImage)
            .where(
                MediaImage.entity_type == db_entity_type,
                MediaImage.entity_id == entity_id,
                MediaImage.is_primary.is_(True),
            )
            .values(is_primary=False)
        )

    # Upload sur Cloudinary (potentielle AppError propagée)
    cloud_data = await cloudinary_service.upload_image(
        file_bytes=file_bytes,
        filename=filename,
        tenant_slug=tenant_slug,
        entity_type=entity_type,
        entity_id=entity_id,
        alt_text=alt_text,
    )
    public_id = cloud_data["cloudinary_public_id"]
    expected_prefix = _expected_cloudinary_prefix(tenant_slug, entity_type, entity_id)
    if not public_id.startswith(expected_prefix):
        raise AppError(
            "CLOUDINARY_PUBLIC_ID_MISMATCH",
            "L'image retournee par Cloudinary n'appartient pas au dossier tenant attendu.",
            status_code=502,
        )

    # Calcul du display_order (dernier + 1)
    result = await session.execute(
        select(MediaImage.display_order)
        .where(
            MediaImage.entity_type == db_entity_type,
            MediaImage.entity_id == entity_id,
        )
        .order_by(MediaImage.display_order.desc())
        .limit(1)
    )
    last_order = result.scalar_one_or_none()
    next_order = (last_order + 1) if last_order is not None else 0

    # Auto-promotion : premiere image de l'entite -> is_primary force a True
    if last_order is None:
        is_primary = True

    image = MediaImage(
        entity_type=db_entity_type,
        entity_id=entity_id,
        cloudinary_public_id=cloud_data["cloudinary_public_id"],
        url=cloud_data["url"],
        url_thumbnail=cloud_data["url_thumbnail"],
        url_medium=cloud_data["url_medium"],
        format=cloud_data["format"],
        size_bytes=cloud_data["size_bytes"],
        width=cloud_data["width"],
        height=cloud_data["height"],
        is_primary=is_primary,
        display_order=next_order,
        alt_text=alt_text,
    )
    session.add(image)
    await session.commit()
    await session.refresh(image)
    return image


async def list_entity_images(
    session: AsyncSession, entity_type: str, entity_id: int
) -> list[MediaImage]:
    """Liste les images d'une entité, triées par ``display_order`` croissant.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        entity_type: Slug d'entité URL (``products``, ``categories``, ...).
        entity_id: Identifiant de l'entité.

    Returns:
        Liste d'instances ``MediaImage`` ordonnées.
    """
    db_entity_type = _ENTITY_TYPE_DB_MAP[entity_type]
    result = await session.execute(
        select(MediaImage)
        .where(
            MediaImage.entity_type == db_entity_type,
            MediaImage.entity_id == entity_id,
        )
        .order_by(MediaImage.display_order.asc())
    )
    return list(result.scalars().all())


async def delete_entity_image(
    session: AsyncSession, image_id: int, tenant_slug: str
) -> None:
    """Supprime une image de Cloudinary et de la base de données.

    Si l'image supprimée était la principale (``is_primary=True``), la suivante
    dans l'ordre de ``display_order`` est automatiquement promue.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        image_id: Clé primaire de l'image à supprimer.
        tenant_slug: Slug du tenant (non utilisé ici, conservé pour cohérence API).

    Raises:
        AppError: ``IMAGE_NOT_FOUND`` (404) si l'image est absente.
        AppError: Propagées depuis ``cloudinary_service.delete_image``.
    """
    image = await session.get(MediaImage, image_id)
    if image is None:
        raise AppError("IMAGE_NOT_FOUND", f"Image #{image_id} introuvable.", status_code=404)

    was_primary = image.is_primary
    entity_type = image.entity_type
    entity_id = image.entity_id
    entity_type_url = _ENTITY_TYPE_URL_MAP.get(entity_type)
    if entity_type_url is not None:
        expected_prefix = _expected_cloudinary_prefix(tenant_slug, entity_type_url, entity_id)
        if not image.cloudinary_public_id.startswith(expected_prefix):
            raise AppError(
                "CLOUDINARY_PUBLIC_ID_MISMATCH",
                "L'image stockee ne correspond pas au dossier tenant attendu.",
                status_code=409,
            )

    # Suppression Cloudinary en premier (idempotent si on doit retry)
    await cloudinary_service.delete_image(image.cloudinary_public_id)

    await session.delete(image)
    await session.flush()

    # Auto-promotion de la prochaine image si besoin
    if was_primary:
        next_result = await session.execute(
            select(MediaImage)
            .where(
                MediaImage.entity_type == entity_type,
                MediaImage.entity_id == entity_id,
            )
            .order_by(MediaImage.display_order.asc())
            .limit(1)
        )
        next_image = next_result.scalar_one_or_none()
        if next_image is not None:
            next_image.is_primary = True

    await session.commit()


async def set_primary_image(
    session: AsyncSession, image_id: int, entity_type: str, entity_id: int
) -> MediaImage:
    """Définit une image comme principale pour une entité.

    Retire le flag ``is_primary`` de toutes les autres images de l'entité.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        image_id: Clé primaire de l'image à promouvoir.
        entity_type: Slug d'entité URL (``products``, ``categories``, ...).
        entity_id: Identifiant de l'entité.

    Returns:
        Instance ``MediaImage`` mise à jour.

    Raises:
        AppError: ``IMAGE_NOT_FOUND`` (404) si l'image est absente.
    """
    db_entity_type = _ENTITY_TYPE_DB_MAP[entity_type]

    image = await session.get(MediaImage, image_id)
    if image is None:
        raise AppError("IMAGE_NOT_FOUND", f"Image #{image_id} introuvable.", status_code=404)
    if image.entity_type != db_entity_type or image.entity_id != entity_id:
        raise AppError(
            "IMAGE_ENTITY_MISMATCH",
            "Cette image n'appartient pas a l'entite demandee.",
            status_code=404,
        )

    # Retirer le flag sur toutes les images de l'entité
    await session.execute(
        update(MediaImage)
        .where(
            MediaImage.entity_type == db_entity_type,
            MediaImage.entity_id == entity_id,
        )
        .values(is_primary=False)
    )
    image.is_primary = True
    await session.commit()
    await session.refresh(image)
    return image


async def reorder_images(
    session: AsyncSession,
    image_ids: list[int],
    entity_type: str,
    entity_id: int,
) -> list[MediaImage]:
    """Réordonne les images d'une entité selon la liste fournie.

    Args:
        session: Session SQLAlchemy tenant-scoped.
        image_ids: Liste d'IDs dans l'ordre souhaité (index 0 = premier affiché).
        entity_type: Slug d'entité URL (``products``, ``categories``, ...).
        entity_id: Identifiant de l'entité.

    Returns:
        Liste des images reordonnées, triées par ``display_order`` croissant.
    """
    db_entity_type = _ENTITY_TYPE_DB_MAP[entity_type]

    existing_result = await session.execute(
        select(MediaImage.id)
        .where(MediaImage.entity_type == db_entity_type, MediaImage.entity_id == entity_id)
        .order_by(MediaImage.display_order.asc())
    )
    existing_ids = list(existing_result.scalars())
    if set(existing_ids) != set(image_ids) or len(existing_ids) != len(image_ids):
        raise AppError(
            "IMAGE_REORDER_MISMATCH",
            "La liste doit contenir exactement les images de cette entite.",
            status_code=422,
        )

    for order, img_id in enumerate(image_ids):
        await session.execute(
            update(MediaImage)
            .where(
                MediaImage.id == img_id,
                MediaImage.entity_type == db_entity_type,
                MediaImage.entity_id == entity_id,
            )
            .values(display_order=order)
        )

    await session.commit()
    return await list_entity_images(session, entity_type, entity_id)
