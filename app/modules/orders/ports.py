"""Port OrderSink : abstraction de transmission des commandes vers un hub POS.

Voir docs/superpowers/specs/2026-08-13-pos-order-transmission-design.md pour le
contexte complet. Permet de brancher une implementation HTTP reelle
(``app/modules/orders/hub_client.py::HttpHubOrderClient``) sans coupler le
reste du module a un contrat hub encore hypothetique.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.orders.models import Order


@dataclass(frozen=True)
class HubPushResult:
    """Resultat de la transmission d'une commande au hub.

    Attributes:
        hub_order_id: Identifiant assigne par le hub a l'acceptation, ou None
            si le hub ne renvoie pas encore d'identifiant a la reception.
    """

    hub_order_id: str | None


@dataclass(frozen=True)
class HubStatusResult:
    """Statut d'une commande tel que rapporte par le hub (callback ou poll).

    Attributes:
        status: Statut brut hub (voir HUB_ORDER_STATUS_ORDER dans hub_status.py).
        hub_order_id: Identifiant hub, si different de celui deja connu.
    """

    status: str
    hub_order_id: str | None = None


@runtime_checkable
class OrderSink(Protocol):
    """Port de transmission des commandes vers un hub d'integration POS."""

    async def push_order(
        self, order: Order, private_reference: str, access_token: str
    ) -> HubPushResult:
        """Transmet une commande au hub.

        Args:
            order: Commande a transmettre (schema tenant courant).
            private_reference: Reference privee envoyee au hub -- toujours
                ``order.idempotency_key`` (jamais une nouvelle cle generee).
            access_token: Token OAuth dechiffre de la connexion POS active.

        Returns:
            HubPushResult avec l'identifiant hub eventuellement assigne.

        Raises:
            Exception: toute erreur reseau/HTTP -- geree par l'appelant
                (worker/tasks/order_hub.py::push_order_to_hub).
        """
        ...

    async def fetch_status(
        self, hub_order_id: str | None, private_reference: str, access_token: str
    ) -> HubStatusResult | None:
        """Interroge le hub pour le statut courant d'une commande (reconciliation).

        Args:
            hub_order_id: Identifiant hub si deja connu, sinon None.
            private_reference: Reference privee (``order.idempotency_key``) en repli.
            access_token: Token OAuth dechiffre de la connexion POS active.

        Returns:
            HubStatusResult si le hub connait la commande, None si introuvable.

        Raises:
            Exception: toute erreur reseau/HTTP -- geree par l'appelant
                (worker/tasks/order_hub.py::reconcile_hub_orders).
        """
        ...
