"""Taux de change indicatifs pour l'affichage informatif de prix en devise
etrangere (voir catalog/router.py, ``?display_currency=``).

[⚠️ PROD] Ne sert JAMAIS a calculer un montant reellement facture -- Stripe
charge toujours dans TenantConfig.currency (verrouillee, voir
admin/tenants/service.py::update_config). Ceci est une conversion
AFFICHAGE SEULEMENT pour aider un client etranger a se reperer.

Fournisseur : Frankfurter (https://frankfurter.dev), taux de reference BCE,
gratuit, sans cle API -- pas de nouveau secret a configurer. Degradation
gracieuse systematique : toute erreur (reseau, timeout, format) est loguee
et traitee comme "taux indisponible", jamais une exception qui remonte a
l'appelant (voir get_cached_rate -- retourne None, ne leve jamais).
"""
import logging

import httpx

from app.core.services.cache import get_cached_json, set_cached_json

logger = logging.getLogger(__name__)

_FRANKFURTER_ENDPOINT = "https://api.frankfurter.dev/v1/latest"
# Rafraichi quotidiennement par worker/tasks/fx_rates_sync.py (4h00 UTC) --
# marge de securite sur la fraicheur, bien au-dela des TTL courts (10-60s)
# utilises ailleurs dans ce module pour du contenu qui change par requete.
_CACHE_TTL_SECONDS = 26 * 3600


def _cache_key(base_currency: str) -> str:
    return f"fx:rates:{base_currency.upper()}"


async def fetch_latest_rates(base_currency: str, symbols: list[str] | None = None) -> dict[str, float]:
    """Recupere les taux de change actuels depuis Frankfurter (base_currency -> *).

    Args:
        base_currency: Devise de base (ISO 4217).
        symbols: Devises cibles a restreindre (defaut : toutes celles connues
            de Frankfurter).

    Returns:
        Mapping devise cible -> taux (ex: {"USD": 1.08}).

    Raises:
        httpx.HTTPError: si l'appel echoue -- a l'appelant de degrader
            gracieusement (voir refresh_and_cache_rates).
    """
    params: dict[str, str] = {"base": base_currency.upper()}
    if symbols:
        params["symbols"] = ",".join(sorted(s.upper() for s in symbols))
    async with httpx.AsyncClient() as client:
        response = await client.get(_FRANKFURTER_ENDPOINT, params=params, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    return {code: float(rate) for code, rate in (data.get("rates") or {}).items()}


async def refresh_and_cache_rates(redis, base_currency: str, symbols: list[str] | None = None) -> bool:
    """Recupere et met en cache les taux pour ``base_currency``.

    Ne leve jamais -- toute erreur est loguee et traitee comme un echec de
    rafraichissement (le cache existant, potentiellement perime, reste en
    place jusqu'au prochain essai).

    Returns:
        True si le rafraichissement a reussi, False sinon.
    """
    try:
        rates = await fetch_latest_rates(base_currency, symbols)
    except Exception:
        logger.warning("FX_RATES_FETCH_FAILED: base=%s", base_currency, exc_info=True)
        return False
    await set_cached_json(redis, _cache_key(base_currency), rates, ttl_seconds=_CACHE_TTL_SECONDS)
    return True


async def get_cached_rate(redis, base_currency: str, target_currency: str) -> float | None:
    """Retourne le taux ``base_currency -> target_currency`` depuis le cache.

    Ne leve jamais -- retourne None si le cache est vide/perime/indisponible,
    a l'appelant de degrader gracieusement (ne jamais bloquer une requete
    catalogue pour une fonctionnalite purement indicative).
    """
    base_currency = base_currency.upper()
    target_currency = target_currency.upper()
    if base_currency == target_currency:
        return 1.0
    rates = await get_cached_json(redis, _cache_key(base_currency))
    if not rates:
        return None
    rate = rates.get(target_currency)
    return float(rate) if rate is not None else None
