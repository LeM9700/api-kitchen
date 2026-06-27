"""Schémas Pydantic transversaux réutilisables dans tous les modules."""

from math import ceil
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Paramètres de pagination extraits des query params.

    Attributes:
        page: Numéro de page (1-indexé).
        page_size: Nombre d'éléments par page (max 100).
    """

    page: int = Field(1, ge=1, description="Numéro de page (1-indexé)")
    page_size: int = Field(20, ge=1, le=100, description="Éléments par page")


class PaginatedResponse(BaseModel, Generic[T]):
    """Enveloppe générique pour les réponses paginées.

    Attributes:
        items: Éléments de la page courante.
        total: Nombre total d'éléments toutes pages confondues.
        page: Numéro de la page courante.
        page_size: Taille de la page demandée.
        pages: Nombre total de pages.
    """

    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(
        cls,
        items: list[T],
        total: int,
        pagination: PaginationParams,
    ) -> "PaginatedResponse[T]":
        """Construit une réponse paginée depuis une liste et un total.

        Args:
            items: Éléments de la page courante retournés par le service.
            total: Nombre total d'enregistrements (pour le calcul des pages).
            pagination: Paramètres de pagination (page, page_size).

        Returns:
            Instance ``PaginatedResponse`` complète.
        """
        pages = max(1, ceil(total / pagination.page_size))
        return cls(
            items=items,
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
            pages=pages,
        )
