"""Taux de change indicatifs pour l'affichage informatif de prix en devise
etrangere (voir catalog/router.py, ``?display_currency=``).

[⚠️ PROD] Ne sert JAMAIS a calculer un montant reellement facture -- Stripe
charge toujours dans TenantConfig.currency (verrouillee, voir
admin/tenants/service.py::update_config). Ceci est une conversion
AFFICHAGE SEULEMENT pour aider un client etranger a se reperer.

Fournisseur : open.er-api.com (https://www.exchangerate-api.com/docs/free),
gratuit, sans cle API -- pas de nouveau secret a configurer. Choisi a la
place de Frankfurter (taux BCE) car ce dernier ne couvre pas le RSD (dinar
serbe), la devise reelle du tenant kod-mome -- open.er-api.com couvre ~160
devises, RSD inclus. Degradation gracieuse systematique : toute erreur
(reseau, timeout, format, echec API) est loguee et traitee comme "taux
indisponible", jamais une exception qui remonte a l'appelant (voir
get_cached_rate -- retourne None, ne leve jamais).
"""
import logging

import httpx

from app.core.services.cache import get_cached_json, set_cached_json

logger = logging.getLogger(__name__)

_ER_API_ENDPOINT = "https://open.er-api.com/v6/latest"
# Rafraichi quotidiennement par worker/tasks/fx_rates_sync.py (4h00 UTC) --
# marge de securite sur la fraicheur, bien au-dela des TTL courts (10-60s)
# utilises ailleurs dans ce module pour du contenu qui change par requete.
_CACHE_TTL_SECONDS = 26 * 3600


def _cache_key(base_currency: str) -> str:
    return f"fx:rates:{base_currency.upper()}"


async def fetch_latest_rates(base_currency: str, symbols: list[str] | None = None) -> dict[str, float]:
    """Recupere les taux de change actuels depuis open.er-api.com (base_currency -> *).

    Args:
        base_currency: Devise de base (ISO 4217).
        symbols: Devises cibles a restreindre (defaut : toutes celles
            renvoyees par le fournisseur). L'API ne supporte pas de filtre
            cote serveur (pas de parametre "symbols") -- le filtrage se fait
            cote client sur la reponse complete.

    Returns:
        Mapping devise cible -> taux (ex: {"USD": 1.08}).

    Raises:
        httpx.HTTPError: si l'appel HTTP echoue.
        ValueError: si l'API repond avec un statut d'echec applicatif
            (``result != "success"``).
        -- dans tous les cas, a l'appelant de degrader gracieusement (voir
        refresh_and_cache_rates).
    """
    url = f"{_ER_API_ENDPOINT}/{base_currency.upper()}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    if data.get("result") != "success":
        raise ValueError(f"open.er-api.com a renvoye un echec pour base={base_currency!r}: {data!r}")
    rates = {code: float(rate) for code, rate in (data.get("rates") or {}).items()}
    if symbols:
        wanted = {s.upper() for s in symbols}
        rates = {code: rate for code, rate in rates.items() if code in wanted}
    return rates


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
