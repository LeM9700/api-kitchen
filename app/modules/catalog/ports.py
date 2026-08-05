"""Port CatalogProvider : abstraction lecture/ecriture du catalogue produits.

Permet de brancher, par tenant, une implementation locale (service.py existant)
ou une implementation adaptee a un systeme de caisse externe (POS), selon
``tenant.integration_mode``. Voir
docs/superpowers/specs/2026-08-05-catalog-provider-design.md pour le contexte.
"""

from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.schemas import PaginationParams
from app.modules.catalog.models import Product
from app.modules.catalog.schemas import ProductCreate, ProductSummaryOut, ProductUpdate


@runtime_checkable
class CatalogProvider(Protocol):
    """Port d'acces au catalogue produits d'un tenant."""

    async def get_catalog(
        self, session: AsyncSession, pagination: PaginationParams
    ) -> tuple[list[ProductSummaryOut], int]:
        """Retourne la page de produits actifs du catalogue (listing par defaut).

        Purpose:
            Fournir le listing pagine du catalogue produits, sans filtre de
            recherche, consomme par ``GET /catalog/products``.

        Args:
            session: Session SQLAlchemy deja ouverte sur le schema du tenant
                courant (``get_tenant_session``).
            pagination: Parametres de pagination (page, page_size).

        Returns:
            Tuple ``(resumes de produits, nombre total de produits actifs)``.

        Raises:
            Aucune exception metier specifique : les erreurs de session/DB
            remontent telles quelles.
        """
        ...

    async def create_product(
        self, session: AsyncSession, body: ProductCreate, *, user_id: int | None
    ) -> Product:
        """Cree un nouveau produit dans le catalogue du tenant courant.

        Purpose:
            Ajouter un produit au catalogue, avec journalisation de l'audit
            prix (creation).

        Args:
            session: Session SQLAlchemy du tenant courant.
            body: Donnees du produit a creer.
            user_id: Identifiant de l'utilisateur a l'origine de la creation,
                pour l'audit prix. ``None`` si non disponible.

        Returns:
            L'entite ``Product`` persistee.

        Raises:
            ReadOnlyCatalogError: si le catalogue de ce tenant est en lecture
                seule (mode CONNECTED).
            HTTPException: 422 si ``category_id`` reference une categorie
                inexistante.
        """
        ...

    async def update_product(
        self,
        session: AsyncSession,
        product_id: int,
        body: ProductUpdate,
        *,
        user_id: int | None,
    ) -> Product:
        """Met a jour un produit existant du catalogue.

        Purpose:
            Appliquer une mise a jour partielle sur un produit (prix, statut
            actif, etc.), avec validation de publication et audit prix si le
            prix change.

        Args:
            session: Session SQLAlchemy du tenant courant.
            product_id: Identifiant du produit a mettre a jour.
            body: Champs a modifier (mise a jour partielle).
            user_id: Identifiant de l'utilisateur a l'origine du changement,
                pour l'audit prix. ``None`` si non disponible.

        Returns:
            L'entite ``Product`` mise a jour.

        Raises:
            ReadOnlyCatalogError: si le catalogue de ce tenant est en lecture
                seule (mode CONNECTED).
            AppError: ``PRODUCT_NOT_FOUND`` (404) si ``product_id`` n'existe
                pas.
        """
        ...

    async def delete_product(
        self, session: AsyncSession, product_id: int, *, user_id: int | None
    ) -> None:
        """Supprime (soft-delete) un produit du catalogue.

        Purpose:
            Desactiver un produit (``is_active=False``) plutot que de le
            supprimer physiquement, pour preserver l'historique des commandes
            qui le referencent.

        Args:
            session: Session SQLAlchemy du tenant courant.
            product_id: Identifiant du produit a desactiver.
            user_id: Identifiant de l'utilisateur a l'origine de la
                suppression, pour l'audit prix. ``None`` si non disponible.

        Returns:
            None.

        Raises:
            ReadOnlyCatalogError: si le catalogue de ce tenant est en lecture
                seule (mode CONNECTED).
            AppError: ``PRODUCT_NOT_FOUND`` (404) si ``product_id`` n'existe
                pas.
        """
        ...
