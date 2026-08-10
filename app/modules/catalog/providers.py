"""Adaptateurs concrets du port CatalogProvider."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.schemas import PaginationParams
from app.modules.catalog import service
from app.modules.catalog.allergen.allergen_service import validate_product_for_publication
from app.modules.catalog.exceptions import ReadOnlyCatalogError
from app.modules.catalog.models import Product
from app.modules.catalog.schemas import ProductCreate, ProductSummaryOut, ProductUpdate


class LocalCatalogProvider:
    """Adaptateur STANDALONE : delegue directement a ``service.py``, sans
    modification de logique metier ni d'effets de bord.
    """

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams
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


class ConnectedCatalogProvider:
    """Adaptateur CONNECTED (stub) : les lectures passent, les ecritures sont
    bloquees par ``ReadOnlyCatalogError``.

    Aucun adaptateur POS reel n'existe encore pour ce lot (P9) : voir
    docs/superpowers/specs/2026-08-05-catalog-provider-design.md pour le
    contexte et les limites explicitement assumees.
    """

    def __init__(self) -> None:
        self._local = LocalCatalogProvider()

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams
    ) -> tuple[list[ProductSummaryOut], int]:
        return await self._local.get_catalog(session, pagination)

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
