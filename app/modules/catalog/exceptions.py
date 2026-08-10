"""Exceptions de domaine du module catalogue."""

from app.core.http.errors import AppError


class ReadOnlyCatalogError(AppError):
    """Le catalogue de ce tenant ne peut pas etre modifie depuis cette API.

    Levee par ``ConnectedCatalogProvider`` : quand un tenant est en mode
    CONNECTED, son catalogue est synchronise depuis un systeme de caisse (POS)
    externe qui fait autorite, donc les ecritures via cette API sont refusees.
    """

    def __init__(self) -> None:
        super().__init__(
            code="CATALOG_READ_ONLY",
            detail=(
                "Le catalogue est actuellement synchronisé depuis votre système de "
                "caisse connecté et ne peut pas être modifié ici. Contactez votre "
                "support pour toute modification de produit."
            ),
            status_code=409,
        )
