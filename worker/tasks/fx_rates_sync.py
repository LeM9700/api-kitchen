"""Job cron quotidien : rafraichit le cache Redis des taux de change
indicatifs consommes par catalog/router.py (``?display_currency=``).

Tache GLOBALE, PAS de boucle par tenant -- contrairement a la plupart des
taches worker (voir CLAUDE.md sur get_tenant_session()), les taux de change
ne sont pas tenant-specifiques : un seul jeu de taux par devise de base sert
tous les tenants configures dans cette devise.
"""
import logging

from app.core.services.fx_rates import refresh_and_cache_rates
from app.modules.admin.tenants.schemas import SUPPORTED_CURRENCIES

logger = logging.getLogger(__name__)


async def refresh_fx_rates(ctx) -> None:
    """Rafraichit les taux de change pour chaque devise de SUPPORTED_CURRENCIES.

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``, ``job_try``).
    """
    redis = ctx.get("redis")
    for base_currency in sorted(SUPPORTED_CURRENCIES):
        targets = sorted(SUPPORTED_CURRENCIES - {base_currency})
        ok = await refresh_and_cache_rates(redis, base_currency, symbols=targets)
        if not ok:
            logger.warning("refresh_fx_rates: echec de rafraichissement pour base=%s", base_currency)
