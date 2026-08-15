"""Client HTTP pour transmettre les commandes au hub POS et interroger leur statut.

[HYPOTHESE NON CONFIRMEE] Le format du payload push, de la reponse, et du
format de ``fetch_status`` sont des hypotheses placeholder -- a confirmer avec
le vrai fournisseur du hub avant activation en production. Isole ici pour
qu'un vrai contrat ne necessite de modifier que ce fichier (voir
docs/superpowers/specs/2026-08-13-pos-order-transmission-design.md).
"""
import httpx

from app.core.config import settings
from app.core.services import crypto
from app.modules.orders.models import Order
from app.modules.orders.ports import HubPushResult, HubStatusResult


class HubOrderClientNotConfigured(RuntimeError):
    """Levee quand l'URL push ou statut du hub n'est pas configuree."""


def is_configured() -> bool:
    """Retourne True si la transmission de commandes au hub est configuree."""
    return bool(settings.pos_hub_order_push_url)


def is_status_configured() -> bool:
    """Retourne True si l'interrogation de statut (reconciliation) est configuree."""
    return bool(settings.pos_hub_order_status_url)


def to_hub_payload(order: Order, private_reference: str) -> dict:
    """Construit le payload push envoye au hub pour une commande.

    Args:
        order: Commande a transmettre.
        private_reference: Reference privee -- toujours ``order.idempotency_key``.

    Returns:
        Dictionnaire JSON-serialisable, format hypothese a confirmer.
    """
    return {
        "private_reference": private_reference,
        "order_type": order.order_type,
        "total": float(order.total),
        "table_number": order.table_number,
        "delivery_address": order.delivery_address if order.order_type == "delivery" else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


class HttpHubOrderClient:
    """Implementation HTTP du port ``OrderSink``."""

    async def push_order(self, order: Order, private_reference: str, access_token: str) -> HubPushResult:
        """Transmet une commande au hub via une requete HTTP POST.

        Args:
            order: Commande a transmettre.
            private_reference: Reference privee (``order.idempotency_key``).
            access_token: Token OAuth dechiffre de la connexion POS active.

        Returns:
            HubPushResult avec l'identifiant hub eventuellement renvoye.

        Raises:
            HubOrderClientNotConfigured: si ``pos_hub_order_push_url`` est vide.
            httpx.HTTPError: si l'appel HTTP echoue ou renvoie un statut d'erreur.
        """
        if not is_configured():
            raise HubOrderClientNotConfigured(
                "POS_HUB_ORDER_PUSH_URL n'est pas configure -- impossible de transmettre la commande."
            )
        payload = to_hub_payload(order, private_reference)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.pos_hub_order_push_url,
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15.0,
            )
        response.raise_for_status()
        data = response.json()
        hub_order_id = data.get("hub_order_id")
        return HubPushResult(hub_order_id=str(hub_order_id) if hub_order_id else None)

    async def fetch_status(
        self, hub_order_id: str | None, private_reference: str, access_token: str
    ) -> HubStatusResult | None:
        """Interroge le statut courant d'une commande aupres du hub.

        Args:
            hub_order_id: Identifiant hub si deja connu, sinon None.
            private_reference: Reference privee (``order.idempotency_key``) en repli.
            access_token: Token OAuth dechiffre de la connexion POS active.

        Returns:
            HubStatusResult si le hub connait la commande, None si introuvable (404).

        Raises:
            HubOrderClientNotConfigured: si ``pos_hub_order_status_url`` est vide.
            httpx.HTTPError: si l'appel HTTP echoue (hors 404).
        """
        if not is_status_configured():
            raise HubOrderClientNotConfigured(
                "POS_HUB_ORDER_STATUS_URL n'est pas configure -- impossible d'interroger le statut."
            )
        params = {"hub_order_id": hub_order_id} if hub_order_id else {"private_reference": private_reference}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                settings.pos_hub_order_status_url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if not status:
            return None
        return HubStatusResult(status=status, hub_order_id=data.get("hub_order_id"))


def decrypt_access_token(access_token_encrypted: str) -> str:
    """Dechiffre le token d'acces d'une connexion POS pour un appel hub.

    Args:
        access_token_encrypted: Valeur chiffree telle que stockee en base.

    Returns:
        Token en clair, jamais loggue par l'appelant.
    """
    return crypto.decrypt_secret(access_token_encrypted)
