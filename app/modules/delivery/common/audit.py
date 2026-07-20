"""Helper d'audit pour le futur reseau de livreurs independants (plans 02-05).

Aucune table d'audit `delivery_*` n'existe encore (ce plan n'ajoute aucune
migration) : cette fonction journalise une transition d'etat de facon
structuree via le logger applicatif standard (voir
`app/core/http/logging_config.py`, qui serialise les champs `extra={...}` en
JSON avec correlation `request_id`). Elle sert de point de passage unique
pour un futur stockage persistant, sans imposer aujourd'hui un modele de
donnees ou une route.

Contrainte forte posee par ce plan : l'appelant ne doit JAMAIS pouvoir faire
fuiter des donnees sensibles (adresse, coordonnees GPS, token, contenu de
document, secret Stripe...) dans les metadonnees auditees. Une allowlist
stricte de cles est appliquee ; toute cle absente est refusee explicitement
(exception), jamais filtree en silence.
"""

import logging
from typing import Any

from app.modules.delivery.common.enums import DeliveryAuditActor
from app.modules.delivery.common.errors import ForbiddenAuditMetadataError

logger = logging.getLogger(__name__)

# Allowlist stricte des cles de metadonnees autorisees dans un evenement
# d'audit du reseau livreur. Toute cle absente de cet ensemble est refusee
# explicitement par `record_delivery_audit_event` -- jamais ignoree en
# silence. Etendre cette liste doit rester une decision explicite et revue
# (elle ne doit jamais accueillir une adresse, des coordonnees GPS, un
# token, du contenu de document ou un secret Stripe), pas un ajout au fil de
# l'eau par un appelant des plans 02-05.
ALLOWED_AUDIT_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "reason",
        "previous_status",
        "new_status",
        "zone_id",
        "order_id",
        "vehicle_type",
        "channel",
        "retry_count",
    }
)


def _assert_allowed_metadata(metadata: dict[str, Any]) -> None:
    forbidden = [key for key in metadata if key not in ALLOWED_AUDIT_METADATA_KEYS]
    if forbidden:
        raise ForbiddenAuditMetadataError(forbidden)


def record_delivery_audit_event(
    *,
    actor: DeliveryAuditActor,
    actor_id: int | str | None,
    entity_type: str,
    entity_id: int | str,
    transition: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Journalise une transition d'etat du reseau de livreurs independants.

    Args:
        actor: Type d'acteur a l'origine de la transition (`DeliveryAuditActor`).
        actor_id: Identifiant de l'acteur (id utilisateur/livreur). `None`
            est attendu pour `DeliveryAuditActor.SYSTEM`.
        entity_type: Type de l'entite concernee (ex: "delivery_request",
            "courier_application").
        entity_id: Identifiant de l'entite concernee.
        transition: Libelle court de la transition (ex: "pending->accepted").
        metadata: Metadonnees additionnelles optionnelles. Doit respecter
            strictement `ALLOWED_AUDIT_METADATA_KEYS` -- toute cle absente
            de cette allowlist (adresse, gps, token, document, secret
            Stripe...) fait lever `ForbiddenAuditMetadataError`.

    Raises:
        ForbiddenAuditMetadataError: si `metadata` contient une cle hors
            allowlist.
    """
    metadata = metadata or {}
    _assert_allowed_metadata(metadata)

    logger.info(
        "delivery_audit_event",
        extra={
            "audit_actor": actor.value,
            "audit_actor_id": actor_id,
            "audit_entity_type": entity_type,
            "audit_entity_id": entity_id,
            "audit_transition": transition,
            "audit_metadata": metadata,
        },
    )
