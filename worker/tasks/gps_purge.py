"""Cron ARQ : purge quotidienne de l'historique GPS des livreurs.

Supprime, pour chaque tenant, les positions plus vieilles que la duree de conservation
(``gps_retention_hours``, plancher de 96 h : voir ``app.modules.delivery.tracking``).
"""
import logging

from app.core.database import engine, get_tenant_session
from app.modules.delivery import tracking
from worker.tasks.stats import _get_all_tenant_slugs

logger = logging.getLogger(__name__)


async def purge_driver_locations(ctx) -> dict:
    """Purge les positions GPS expirees de tous les tenants. Une erreur sur un tenant ne bloque pas les autres."""
    totals = {"tenants": 0, "points": 0, "last_locations": 0, "errors": 0}
    for slug in await _get_all_tenant_slugs(engine):
        try:
            async with get_tenant_session(slug) as session:
                result = await tracking.purge_old_locations(session)
            totals["tenants"] += 1
            totals["points"] += result["points"]
            totals["last_locations"] += result["last_locations"]
        except Exception:
            totals["errors"] += 1
            logger.exception("gps purge failed for tenant %s", slug)
    logger.info("gps purge done: %s", totals)
    return totals
