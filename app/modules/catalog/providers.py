"""Adaptateurs concrets du port CatalogProvider."""

import logging
import zlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.schemas import PaginationParams
from app.modules.catalog import service, snapshot_repository
from app.modules.catalog.allergen.allergen_service import validate_product_for_publication
from app.modules.catalog.exceptions import CatalogSnapshotUnavailableError, ReadOnlyCatalogError
from app.modules.catalog.models import Product, ProductOverride
from app.modules.catalog.schemas import (
    NormalizedCatalogProduct,
    ProductCreate,
    ProductSummaryOut,
    ProductUpdate,
)

logger = logging.getLogger(__name__)

# Rang de tri applique aux produits sans display_order explicite : ils passent
# apres tous les produits ordonnes manuellement, puis sont tries par nom.
_UNORDERED_RANK = 9999


class LocalCatalogProvider:
    """Adaptateur STANDALONE : delegue directement a ``service.py``, sans
    modification de logique metier ni d'effets de bord.
    """

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams, redis=None
    ) -> tuple[list[ProductSummaryOut], int]:
        items, total = await service.list_products(session, pagination)
        summaries = await service.build_product_summaries(session, items, include_availability=True)
        return summaries, total

    async def create_product(
        self, session: AsyncSession, body: ProductCreate, *, user_id: int | None
    ) -> Product:
        return await service.create_product(session, body, user_id=user_id)

    async def update_product(
        self,
        session: AsyncSession,
        product_id: int,
        body: ProductUpdate,
        *,
        user_id: int | None,
    ) -> Product:
        if body.is_active is True:
            await validate_product_for_publication(session, product_id)
        return await service.update_product(session, product_id, body, user_id=user_id)

    async def delete_product(
        self, session: AsyncSession, product_id: int, *, user_id: int | None
    ) -> None:
        await service.update_product(
            session,
            product_id,
            ProductUpdate(is_active=False),
            user_id=user_id,
        )


def _surrogate_id(external_id: str) -> int:
    """Derive un id entier stable a partir de l'id externe hub (CRC32, 31 bits).

    [LIMITE CONNUE] Mapping deterministe sans registre -- risque de collision
    theorique negligeable pour un catalogue pizzeria (dizaines d'articles). A
    remplacer par une vraie table de correspondance si le futur contrat hub ne
    fournit pas d'id numerique stable.

    Args:
        external_id: Identifiant du produit cote hub.

    Returns:
        Un entier positif (31 bits) stable pour un ``external_id`` donne.
    """
    return zlib.crc32(external_id.encode()) & 0x7FFFFFFF


def _to_summary(item: NormalizedCatalogProduct, override: ProductOverride | None) -> ProductSummaryOut:
    """Projette un produit normalise du snapshot en ``ProductSummaryOut``.

    Seuls les champs de presentation (image, description, mise en avant) peuvent
    etre surcharges localement. ``base_price`` et ``tax_rate`` proviennent
    toujours du snapshot hub : ``ProductOverride`` n'expose deliberement aucune
    colonne prix/TVA (garde-fou fiscal structurel).

    Args:
        item: Produit normalise issu de ``catalog_snapshots.normalized``.
        override: Override de presentation local pour ce produit, ou None.

    Returns:
        Le resume produit expose par l'API.
    """
    image_url = item.image_url
    description = item.description
    is_featured = False
    if override is not None:
        if override.image_url is not None:
            image_url = override.image_url
        if override.description is not None:
            description = override.description
        if override.is_featured is not None:
            is_featured = override.is_featured
    return ProductSummaryOut(
        id=_surrogate_id(item.external_id),
        category_id=None,
        name=item.name,
        description=description,
        base_price=item.price,
        tax_rate=item.tax_rate,
        is_featured=is_featured,
        image_url=image_url,
        preparation_station=None,
        is_active=item.is_active,
        category=None,
        effective_preparation_station="kitchen",
        primary_image=None,
        allergens=[],
        dietary_tags=[],
        availability=None,
        regulatory_complete=False,
    )


class HubCatalogProvider:
    """Adaptateur CONNECTED : sert le catalogue exclusivement depuis
    ``catalog_snapshots`` (jamais d'appel reseau pendant une requete entrante).
    Les ecritures restent bloquees par ``ReadOnlyCatalogError``.

    Remplace l'ancien ``ConnectedCatalogProvider`` (stub P9) -- voir
    docs/superpowers/specs/2026-08-11-hub-catalog-sync-design.md.
    """

    def __init__(self, connection_id: int | None) -> None:
        self._connection_id = connection_id

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams, redis=None
    ) -> tuple[list[ProductSummaryOut], int]:
        """Retourne la page de catalogue construite depuis le snapshot hub.

        Seuls les produits actifs du snapshot sont servis (et comptes dans le
        total), comme ``LocalCatalogProvider``.

        Args:
            session: Session SQLAlchemy positionnee sur le schema du tenant.
            pagination: Parametres de pagination (appliques en memoire : le
                snapshot entier est deja charge sous forme de JSON).
            redis: Pool ARQ optionnel. S'il est fourni et que le snapshot est
                perime, une resynchronisation est enqueue en best-effort.

        Returns:
            Tuple ``(resumes de produits actifs de la page, total des produits
            actifs du snapshot)``.

        Raises:
            CatalogSnapshotUnavailableError: si le tenant CONNECTED n'a pas de
                connexion active, ou si aucun snapshot n'a encore ete synchronise.
        """
        if self._connection_id is None:
            raise CatalogSnapshotUnavailableError()

        snapshot = await snapshot_repository.get_snapshot(session, self._connection_id)
        if snapshot is None:
            raise CatalogSnapshotUnavailableError()

        overrides = await snapshot_repository.list_overrides(session, self._connection_id)
        # Meme semantique que LocalCatalogProvider (service.list_products) : un
        # produit desactive cote caisse sort du listing public ET du total, sans
        # attendre le prochain cycle de synchronisation du snapshot.
        items = [
            item
            for item in (NormalizedCatalogProduct(**entry) for entry in snapshot.normalized)
            if item.is_active
        ]
        items.sort(
            key=lambda item: (
                overrides[item.external_id].display_order
                if item.external_id in overrides and overrides[item.external_id].display_order is not None
                else _UNORDERED_RANK,
                item.name,
            )
        )
        summaries = [_to_summary(item, overrides.get(item.external_id)) for item in items]

        total = len(summaries)
        start = (pagination.page - 1) * pagination.page_size
        end = start + pagination.page_size
        page_items = summaries[start:end]

        staleness = timedelta(minutes=settings.pos_hub_snapshot_staleness_minutes)
        if redis is not None and datetime.now(timezone.utc) - snapshot.synced_at > staleness:
            # Best-effort : un snapshot perime reste servi, une panne Redis ne
            # doit jamais faire echouer la lecture du catalogue.
            try:
                await redis.enqueue_job("sync_catalog_from_hub", connection_id=self._connection_id)
            except Exception:  # noqa: BLE001 - resynchronisation opportuniste
                logger.warning(
                    "hub_catalog_stale_resync_enqueue_failed connection_id=%s",
                    self._connection_id,
                    exc_info=True,
                )

        return page_items, total

    async def create_product(
        self, session: AsyncSession, body: ProductCreate, *, user_id: int | None
    ) -> Product:
        raise ReadOnlyCatalogError()

    async def update_product(
        self,
        session: AsyncSession,
        product_id: int,
        body: ProductUpdate,
        *,
        user_id: int | None,
    ) -> Product:
        raise ReadOnlyCatalogError()

    async def delete_product(
        self, session: AsyncSession, product_id: int, *, user_id: int | None
    ) -> None:
        raise ReadOnlyCatalogError()
