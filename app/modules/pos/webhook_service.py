"""Verification de signature et validation de connexion pour le webhook
inventaire du hub POS (``POST /pos/catalog-webhook/{connection_id}``).

[HubRise] Signature : header ``X-HubRise-Hmac-SHA256``, HMAC-SHA256 sur le
corps brut, signe avec le ``client_secret`` OAuth (pas de secret webhook
dedie) -- voir https://www.hubrise.com/developers/api/callbacks.

Le payload de callback HubRise ne porte aucun identifiant de location
(seulement ``resource_type``/``event_type``/id de ressource) : c'est
pourquoi la resolution du tenant se fait via l'URL de callback elle-meme
(``connection_id`` dans le chemin, enregistree par connexion -- voir
``app.modules.pos.service.register_hub_callback``), pas depuis le corps.
"""
import hashlib
import hmac

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_public_session

WEBHOOK_SIGNATURE_HEADER = "X-HubRise-Hmac-SHA256"


def is_webhook_configured() -> bool:
    """Retourne True si le client_secret (utilise pour signer les callbacks) est configure."""
    return bool(settings.pos_hub_client_secret)


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifie la signature HMAC-SHA256 du corps brut de la requete webhook.

    Args:
        raw_body: Corps brut (bytes) de la requete, avant tout parsing JSON.
        signature_header: Valeur du header ``X-HubRise-Hmac-SHA256``.

    Returns:
        True si la signature est valide, False sinon (y compris si absente).
    """
    if not signature_header:
        return False
    expected = hmac.new(settings.pos_hub_client_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def is_connection_active(connection_id: int) -> bool:
    """Verifie que ``connection_id`` (issu de l'URL de callback) est une
    connexion POS active.

    Args:
        connection_id: Id de la ligne ``public.pos_connections``, tel
            qu'embarque dans l'URL de callback enregistree pour cette
            connexion.

    Returns:
        True si la connexion existe et est active.
    """
    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT 1 FROM public.pos_connections WHERE id = :id AND status = 'active'"),
            {"id": connection_id},
        )
        return result.scalar_one_or_none() is not None
