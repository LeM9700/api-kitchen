"""SQLAlchemy event listeners pour la validation des allergènes à la publication.

[🔒 SÉCURITÉ] Couche de défense 1 sur 2 — bloque la publication d'un produit si
les 14 allergènes réglementaires ne sont pas tous déclarés.

Ce module doit être importé au démarrage de l'application (dans main.py via lifespan)
pour que les listeners soient enregistrés. L'import seul suffit — aucune fonction à
appeler.

Note: Les SQLAlchemy `before_update` events sont synchrones (core events) même dans
un contexte async. On utilise `connection` (Core) pour la requête de vérification,
ce qui est compatible avec le moteur asyncpg via `op.get_bind()` en contexte sync.
"""

import logging

from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.orm import attributes

from app.modules.catalog.models import Product

logger = logging.getLogger(__name__)


@sa_event.listens_for(Product, "before_update")
def enforce_allergen_validation_on_publish(mapper, connection, target: Product) -> None:
    """[🔒 SÉCURITÉ] Bloque la publication d'un produit si des allergènes réglementaires
    ne sont pas déclarés.

    Déclenché automatiquement par SQLAlchemy avant chaque UPDATE sur Product.
    N'agit que lorsque `is_active` passe de False → True (publication effective).

    Args:
        mapper: Mapper SQLAlchemy (non utilisé directement).
        connection: Connexion Core synchrone fournie par l'event.
        target: Instance Product en cours de mise à jour.

    Raises:
        ValueError: Si des allergènes réglementaires ne sont pas déclarés.
            L'exception remonte jusqu'au handler FastAPI qui la convertit en 422.
    """
    history = attributes.get_history(target, "is_active")

    # Détermine si on passe de inactif → actif
    was_inactive = bool(history.deleted) and not history.deleted[0]
    becomes_active = bool(history.added) and history.added[0]

    if not (was_inactive and becomes_active):
        return

    # [🔒 SÉCURITÉ] Vérification synchrone via connection Core
    result = connection.execute(
        text("""
            SELECT COUNT(*) FROM allergen_definitions ad
            WHERE ad.is_regulatory = TRUE
            AND NOT EXISTS (
                SELECT 1 FROM product_allergens pa
                WHERE pa.product_id = :product_id AND pa.allergen_id = ad.id
            )
        """),
        {"product_id": target.id},
    )
    missing_count = result.scalar() or 0

    if missing_count > 0:
        logger.warning(
            "Publication bloquée : product_id=%s, %d allergène(s) réglementaire(s) manquant(s)",
            target.id,
            missing_count,
        )
        raise ValueError(
            f"Impossible de publier ce produit : {missing_count} allergène(s) "
            f"réglementaire(s) non déclarés. Complétez la fiche allergènes."
        )
