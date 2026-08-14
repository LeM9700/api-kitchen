"""Adaptateurs concrets du port CatalogProvider."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.schemas import PaginationParams
from app.modules.catalog import hub_client, override_repository, service, snapshot_repository
from app.modules.catalog.allergen.allergen_service import validate_product_for_publication
from app.modules.catalog.exceptions import CatalogSnapshotUnavailableError, ReadOnlyCatalogError
from app.modules.catalog.models import Product, ProductOverride
from app.modules.catalog.schemas import ProductCreate, ProductSummaryOut, ProductUpdate

logger = logging.getLogger(__name__)


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


def _apply_override(summary: ProductSummaryOut, override: ProductOverride | None) -> ProductSummaryOut:
    """Fusionne les champs de presentation d'un override sur un resume produit.

    ``base_price``/``tax_rate`` ne sont jamais touches : ``ProductOverride``
    n'expose structurellement aucune colonne prix/TVA (garde-fou fiscal).

    Args:
        summary: Resume produit issu de ``LocalCatalogProvider.get_catalog``.
        override: Override de presentation local pour ce produit, ou None.

    Returns:
        Le meme ``summary``, avec les champs de presentation non-None de
        l'override appliques par-dessus.
    """
    if override is None:
        return summary
    if override.image_url is not None:
        summary.image_url = override.image_url
    if override.description is not None:
        summary.description = override.description
    if override.is_featured is not None:
        summary.is_featured = override.is_featured
    return summary


class HubCatalogProvider:
    """Adaptateur CONNECTED : delegue la lecture a ``LocalCatalogProvider``
    (donc a ``products``, materialise par
    ``worker/tasks/catalog_sync.py::sync_catalog_from_hub``) et fusionne les
    ``product_overrides`` de presentation par-dessus. Les ecritures restent
    bloquees par ``ReadOnlyCatalogError``.

    Voir docs/superpowers/specs/2026-08-12-hub-catalog-materialization-design.md.
    """

    def __init__(self, connection_id: int | None) -> None:
        self._connection_id = connection_id

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams, redis=None
    ) -> tuple[list[ProductSummaryOut], int]:
        """Retourne la page de catalogue lue depuis ``products``, overrides fusionnes.

        Args:
            session: Session SQLAlchemy positionnee sur le schema du tenant.
            pagination: Parametres de pagination, transmis tels quels a
                ``LocalCatalogProvider`` (pagination SQL reelle, plus en memoire).
            redis: Pool ARQ optionnel. S'il est fourni et que le snapshot est
                perime (mais pas encore expire), une resynchronisation est
                enqueue en best-effort.

        Returns:
            Tuple ``(resumes de produits actifs de la page, total des produits
            actifs)``.

        Raises:
            CatalogSnapshotUnavailableError: si le tenant CONNECTED n'a pas de
                connexion active, si aucun snapshot n'a encore ete synchronise,
                ou si le dernier snapshot depasse
                ``settings.pos_hub_snapshot_hard_expiry_hours``.
        """
        if self._connection_id is None:
            raise CatalogSnapshotUnavailableError()

        snapshot = await snapshot_repository.get_snapshot(session, self._connection_id)
        if snapshot is None:
            raise CatalogSnapshotUnavailableError()

        age = datetime.now(timezone.utc) - snapshot.synced_at
        hard_expiry = timedelta(hours=settings.pos_hub_snapshot_hard_expiry_hours)
        if age > hard_expiry:
            raise CatalogSnapshotUnavailableError()

        summaries, total = await LocalCatalogProvider().get_catalog(session, pagination)
        overrides = await override_repository.list_overrides_by_product_ids(
            session, [s.id for s in summaries]
        )
        summaries = [_apply_override(s, overrides.get(s.id)) for s in summaries]

        staleness = timedelta(minutes=settings.pos_hub_snapshot_staleness_minutes)
        if redis is not None and hub_client.is_configured() and age > staleness:
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

        return summaries, total

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
