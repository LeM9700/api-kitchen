"""Cache applicatif court-terme (Redis) pour les lectures a fort volume/faible
frequence de changement -- listing catalogue, statut tenant.

[PERF] Utilise le pool arq partage (``app.state.arq_pool``) plutot qu'une
connexion Redis dediee -- evite d'ouvrir un second pool pour un besoin qui
reste marginal. Les TTL courts (15-60s) bornent la fraicheur des donnees sans
necessiter une invalidation exhaustive de chaque chemin de mutation.
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def get_cached_json(redis, key: str) -> Any | None:
    """Retourne la valeur JSON en cache pour ``key``, ou ``None`` si absente/invalide.

    Ne leve jamais -- un cache indisponible ou corrompu ne doit jamais casser
    la requete, seulement faire perdre le benefice du cache pour cet appel.
    """
    try:
        raw = await redis.get(key)
    except Exception:
        logger.warning("CACHE_READ_FAILED: key=%s", key, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("CACHE_DECODE_FAILED: key=%s", key)
        return None


async def set_cached_json(redis, key: str, value: Any, ttl_seconds: int) -> None:
    """Ecrit ``value`` (serialisable JSON) en cache sous ``key`` avec un TTL.

    Ne leve jamais -- un echec d'ecriture cache degrade juste vers un cache miss
    au prochain appel, ce n'est jamais une erreur bloquante pour la requete.
    """
    try:
        await redis.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception:
        logger.warning("CACHE_WRITE_FAILED: key=%s", key, exc_info=True)


async def invalidate_prefix(redis, prefix: str) -> None:
    """Supprime toutes les cles commencant par ``prefix`` (invalidation catalogue/tenant).

    Utilise ``SCAN`` (pas ``KEYS``) pour ne pas bloquer Redis sur un dataset
    volumineux -- acceptable ici car le volume de cles cache reste faible
    (quelques pages par tenant).
    """
    try:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}*")]
        if keys:
            await redis.delete(*keys)
    except Exception:
        logger.warning("CACHE_INVALIDATE_FAILED: prefix=%s", prefix, exc_info=True)
