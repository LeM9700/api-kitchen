from enum import Enum


class IntegrationMode(str, Enum):
    """Mode d'integration d'un tenant avec le systeme de vente.

    Determine qui est le systeme de verite pour les commandes et le stock
    d'un tenant. Purement declaratif au niveau infrastructure (public.tenants)
    pour ce lot : aucune branche de code applicative ne lit encore cette
    valeur, aucune route n'est modifiee.

    Attributes:
        STANDALONE: api-kitchen est le systeme de verite exclusif du tenant.
            Toutes les commandes sont creees, transitionnees et closes via ce
            systeme (flux internes existants, inchange). C'est le mode par
            defaut : tous les tenants crees avant l'introduction de ce champ
            restent en STANDALONE sans aucun changement de comportement.
        CONNECTED: le tenant utilise une caisse certifiee externe (POS) comme
            systeme de verite. La liaison technique (identifiants, jetons
            d'acces, perimetre de droits) est stockee dans
            ``public.pos_connections``. En CONNECTED, api-kitchen doit a terme
            accepter des transitions de statut imposees par le POS externe
            sans les rejeter meme hors du graphe de transitions internes
            (cf. ``TransitionAuthority.EXTERNAL`` dans
            ``app.modules.orders.service``) — ce comportement applicatif n'est
            pas implemente dans ce lot, seule la donnee d'infrastructure
            necessaire pour le porter plus tard est posee ici.
    """

    STANDALONE = "standalone"
    CONNECTED = "connected"
