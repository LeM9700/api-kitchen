"""Acces DB au snapshot catalogue hub et aux overrides de presentation locaux.

Toutes les fonctions attendent une session deja positionnee sur le schema du
tenant courant (``get_tenant_session`` / ``SET search_path``) -- meme
convention que le reste du module catalogue.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import CatalogSnapshot, ProductOverride
from app.modules.catalog.schemas import NormalizedCatalogProduct


async def get_snapshot(session: AsyncSession, connection_id: int) -> CatalogSnapshot | None:
    """Retourne le snapshot catalogue courant d'une connexion, ou None.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        connection_id: Identifiant ``public.pos_connections.id``.

    Returns:
        Le ``CatalogSnapshot`` existant, ou None si aucune sync n'a encore eu lieu.
    """
    result = await session.execute(
        select(CatalogSnapshot).where(CatalogSnapshot.connection_id == connection_id)
    )
    return result.scalar_one_or_none()


async def upsert_snapshot(
    session: AsyncSession,
    connection_id: int,
    payload: dict,
    normalized: list[NormalizedCatalogProduct],
) -> CatalogSnapshot:
    """Cree ou met a jour le snapshot catalogue d'une connexion.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        connection_id: Identifiant ``public.pos_connections.id``.
        payload: Reponse brute du hub (audit/debug).
        normalized: Catalogue normalise (format pivot) tel que retourne par
            ``normalize_catalog``.

    Returns:
        Le ``CatalogSnapshot`` persiste (cree ou mis a jour), avec ``synced_at``
        rafraichi a l'heure courante.
    """
    normalized_json = [item.model_dump(mode="json") for item in normalized]
    snapshot = await get_snapshot(session, connection_id)
    now = datetime.now(timezone.utc)
    if snapshot is None:
        snapshot = CatalogSnapshot(
            connection_id=connection_id,
            payload=payload,
            normalized=normalized_json,
            synced_at=now,
        )
        session.add(snapshot)
    else:
        snapshot.payload = payload
        snapshot.normalized = normalized_json
        snapshot.synced_at = now
    await session.commit()
    return snapshot


async def list_overrides(session: AsyncSession, connection_id: int) -> dict[str, ProductOverride]:
    """Retourne les overrides de presentation d'une connexion, indexes par id externe.

    Args:
        session: Session SQLAlchemy positionnee sur le schema du tenant.
        connection_id: Identifiant ``public.pos_connections.id``.

    Returns:
        Dictionnaire ``{external_product_id: ProductOverride}``.
    """
    result = await session.execute(
        select(ProductOverride).where(ProductOverride.connection_id == connection_id)
    )
    return {override.external_product_id: override for override in result.scalars()}
