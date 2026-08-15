"""Logique partagee d'application d'un statut hub a une commande.

Utilisee a la fois par le receiver de callback et par le job de reconciliation
(``worker/tasks/order_hub.py``) pour ne jamais dupliquer le controle
anti-regression -- voir
docs/superpowers/specs/2026-08-13-pos-order-transmission-design.md.

[HYPOTHESE NON CONFIRMEE] ``HUB_ORDER_STATUS_ORDER`` et
``HUB_STATUS_TO_ORDER_STATUS`` sont des hypotheses placeholder, a confirmer
avec la documentation reelle du fournisseur du hub avant mise en production.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order, OrderHubTransmission
from app.modules.orders.service import TransitionAuthority, update_status

logger = logging.getLogger(__name__)

# Ordre de preseance des statuts hub, du moins avance au plus avance. Un
# statut absent de cette liste est traite comme rang -1 (jamais plus avance
# qu'un statut deja connu) -- choix conservateur : un statut inconnu ne
# regresse ni ne fait progresser silencieusement une commande.
HUB_ORDER_STATUS_ORDER: list[str] = [
    "received",
    "accepted",
    "preparing",
    "ready",
    "rejected",
    "cancelled",
]

# Statuts hub qui declenchent une transition de Order.status. Les statuts
# absents de ce mapping (ex: "received", "preparing", "ready") ne font
# qu'actualiser last_hub_status -- VALID_TRANSITIONS ne represente pas ces
# etats intermediaires cote metier.
HUB_STATUS_TO_ORDER_STATUS: dict[str, str] = {
    "accepted": "confirmed",
    "rejected": "rejected",
    "cancelled": "cancelled",
}


def _rank(status: str | None) -> int:
    if status is None:
        return -1
    try:
        return HUB_ORDER_STATUS_ORDER.index(status)
    except ValueError:
        return -1


async def apply_hub_status(
    session: AsyncSession,
    order_id: int,
    hub_status: str,
    *,
    tenant_slug: str,
    hub_order_id: str | None = None,
    source: str = "callback",
    arq_pool=None,
) -> None:
    """Applique un statut hub a une commande, sans jamais la faire regresser.

    Point d'entree unique pour toute transition d'origine hub (callback ou
    reconciliation) -- garantit que les deux ne peuvent jamais se contredire.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        order_id: Identifiant de la commande cible.
        hub_status: Statut brut recu du hub.
        tenant_slug: Slug tenant, requis par ``update_status`` pour confirmed/cancelled.
        hub_order_id: Identifiant hub, renseigne sur la transmission si connu.
        source: "callback" ou "reconciliation", pour la note d'historique et les logs.
        arq_pool: Pool arq partage, transmis a ``update_status`` pour ses
            notifications post-commit (best-effort, jamais bloquant).
    """
    transmission = await session.scalar(
        select(OrderHubTransmission).where(OrderHubTransmission.order_id == order_id)
    )
    if transmission is None:
        logger.warning(
            "apply_hub_status: aucune transmission trouvee order_id=%s source=%s", order_id, source
        )
        return

    if hub_order_id and not transmission.hub_order_id:
        transmission.hub_order_id = hub_order_id

    new_rank = _rank(hub_status)
    old_rank = _rank(transmission.last_hub_status)
    if new_rank <= old_rank:
        logger.info(
            "apply_hub_status: stale_status order_id=%s hub_status=%s last_hub_status=%s source=%s",
            order_id,
            hub_status,
            transmission.last_hub_status,
            source,
        )
        await session.commit()
        return

    transmission.last_hub_status = hub_status
    transmission.acknowledged_at = datetime.now(timezone.utc)
    transmission.transmission_status = "acknowledged"

    mapped_status = HUB_STATUS_TO_ORDER_STATUS.get(hub_status)
    if mapped_status is None:
        await session.commit()
        logger.info(
            "apply_hub_status: statut intermediaire order_id=%s hub_status=%s source=%s",
            order_id,
            hub_status,
            source,
        )
        return

    order = await session.get(Order, order_id)
    if order is None:
        logger.warning("apply_hub_status: order_id=%s introuvable source=%s", order_id, source)
        await session.commit()
        return

    await update_status(
        session,
        order_id,
        mapped_status,
        f"Hub status update ({source}): {hub_status}",
        tenant_slug=tenant_slug,
        arq_pool=arq_pool,
        authority=TransitionAuthority.EXTERNAL,
    )
