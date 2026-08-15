"""Verification de signature et resolution de connexion pour le webhook
inventaire du hub POS (``POST /pos/catalog-webhook``).

[HYPOTHESE NON CONFIRMEE] Le format de signature (HMAC-SHA256 sur le corps
brut, header ``X-Hub-Signature``) et la cle utilisee
(``settings.pos_hub_webhook_secret``) sont des hypotheses placeholder -- a
confirmer avec le vrai fournisseur du hub avant activation en production.
"""
import hashlib
import hmac

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_public_session


def is_webhook_configured() -> bool:
    """Retourne True si le secret de signature webhook est configure."""
    return bool(settings.pos_hub_webhook_secret)


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifie la signature HMAC-SHA256 du corps brut de la requete webhook.

    Args:
        raw_body: Corps brut (bytes) de la requete, avant tout parsing JSON.
        signature_header: Valeur du header de signature envoye par le hub.

    Returns:
        True si la signature est valide, False sinon (y compris si absente).
    """
    if not signature_header:
        return False
    expected = hmac.new(settings.pos_hub_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def resolve_connection_id(external_establishment_id: str) -> int | None:
    """Retrouve l'id de connexion POS active correspondant a l'etablissement hub.

    Args:
        external_establishment_id: Identifiant d'etablissement envoye par le
            hub dans le payload webhook.

    Returns:
        L'id de la connexion active, ou None si introuvable.
    """
    async with get_public_session() as session:
        return await session.scalar(
            text(
                "SELECT id FROM public.pos_connections "
                "WHERE external_establishment_id = :external_id AND status = 'active'"
            ),
            {"external_id": external_establishment_id},
        )


def is_order_webhook_configured() -> bool:
    """Retourne True si le secret de signature du webhook commandes est configure."""
    return bool(settings.pos_hub_order_webhook_secret)


def verify_order_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifie la signature HMAC-SHA256 du corps brut du webhook commandes.

    Secret distinct de celui du webhook catalogue
    (``pos_hub_order_webhook_secret`` vs ``pos_hub_webhook_secret``).

    Args:
        raw_body: Corps brut (bytes) de la requete, avant tout parsing JSON.
        signature_header: Valeur du header de signature envoye par le hub.

    Returns:
        True si la signature est valide, False sinon (y compris si absente).
    """
    if not signature_header:
        return False
    expected = hmac.new(settings.pos_hub_order_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def resolve_order_context(external_establishment_id: str) -> dict | None:
    """Retrouve la connexion active et le tenant correspondant a un etablissement hub.

    Utilise par ``process_hub_order_callback`` pour determiner dans quel
    schema tenant resoudre la commande, une fois le payload du callback
    commandes parse (jamais avant -- le corps est mis en file brut, voir
    ``app/modules/pos/router.py::order_webhook``).

    Args:
        external_establishment_id: Identifiant d'etablissement envoye par le
            hub dans le payload du callback.

    Returns:
        Mapping avec ``connection_id`` et ``tenant_slug``, ou None si
        l'etablissement est inconnu ou la connexion n'est plus active.
    """
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT pc.id AS connection_id, t.slug AS tenant_slug "
                "FROM public.pos_connections pc "
                "JOIN public.tenants t ON t.id = pc.tenant_id "
                "WHERE pc.external_establishment_id = :external_id AND pc.status = 'active'"
            ),
            {"external_id": external_establishment_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None
