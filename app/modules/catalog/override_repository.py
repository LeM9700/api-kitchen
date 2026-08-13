"""Acces DB aux overrides de presentation locaux (product_overrides), cles
sur product_id -- meme convention de session que snapshot_repository.py.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import ProductOverride
from app.modules.catalog.schemas import ProductOverrideCreate


async def get_override(session: AsyncSession, product_id: int) -> ProductOverride | None:
    """Retourne l'override de presentation d'un produit, ou None.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        product_id: Identifiant du produit (``products.id``).

    Returns:
        Le ``ProductOverride`` existant, ou None si aucun n'a ete cree.
    """
    result = await session.execute(select(ProductOverride).where(ProductOverride.product_id == product_id))
    return result.scalar_one_or_none()


async def list_overrides_by_product_ids(
    session: AsyncSession, product_ids: list[int]
) -> dict[int, ProductOverride]:
    """Retourne les overrides existants pour un ensemble de produits.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        product_ids: Identifiants de produits a rechercher.

    Returns:
        Dictionnaire ``{product_id: ProductOverride}`` -- seuls les produits
        ayant reellement un override sont presents.
    """
    if not product_ids:
        return {}
    result = await session.execute(select(ProductOverride).where(ProductOverride.product_id.in_(product_ids)))
    return {override.product_id: override for override in result.scalars()}


async def upsert_override(
    session: AsyncSession, product_id: int, body: ProductOverrideCreate
) -> ProductOverride:
    """Cree ou remplace l'override de presentation d'un produit.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        product_id: Identifiant du produit a surcharger.
        body: Champs de presentation (image/description/mise en avant/ordre).

    Returns:
        Le ``ProductOverride`` persiste (cree ou mis a jour).
    """
    override = await get_override(session, product_id)
    if override is None:
        override = ProductOverride(product_id=product_id, **body.model_dump())
        session.add(override)
    else:
        for field, value in body.model_dump().items():
            setattr(override, field, value)
    await session.commit()
    return override


async def delete_override(session: AsyncSession, product_id: int) -> bool:
    """Supprime l'override de presentation d'un produit, s'il existe.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        product_id: Identifiant du produit.

    Returns:
        True si un override a ete supprime, False s'il n'y en avait aucun.
    """
    override = await get_override(session, product_id)
    if override is None:
        return False
    await session.delete(override)
    await session.commit()
    return True
