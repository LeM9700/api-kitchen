"""Garde-fous Redis pour la synchronisation catalogue hub : limite de debit
(fenetre fixe par minute) et verrou anti-concurrence par connexion.

Aucune de ces fonctions ne leve jamais sur un probleme Redis inattendu -- un
appelant (worker task) doit gerer explicitement le cas ou redis est
indisponible plutot que de s'appuyer sur une exception ici.
"""
import time

_RATE_LIMIT_KEY_PREFIX = "pos_hub_ratelimit:"
_LOCK_KEY_PREFIX = "pos_catalog_sync_lock:"


async def check_rate_limit(redis, connection_id: int, limit_per_minute: int) -> bool:
    """Verifie et consomme un slot dans la fenetre de la minute courante.

    Fenetre fixe (horloge murale, pas glissante) : simple et suffisant pour un
    budget de quelques requetes/minute, avec un leger effet de bord possible a
    la frontiere de deux minutes (accepte, documente dans le design spec).

    Args:
        redis: Client Redis partage (ArqRedis ou double de test).
        connection_id: Identifiant de connexion POS a limiter.
        limit_per_minute: Nombre maximum d'appels autorises par minute.

    Returns:
        True si l'appel est autorise (slot consomme), False si la limite est atteinte.
    """
    window = int(time.time() // 60)
    key = f"{_RATE_LIMIT_KEY_PREFIX}{connection_id}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    return count <= limit_per_minute


async def acquire_sync_lock(redis, connection_id: int, ttl_seconds: int) -> bool:
    """Acquiert un verrou exclusif pour la synchronisation d'une connexion.

    Args:
        redis: Client Redis partage.
        connection_id: Identifiant de connexion POS a verrouiller.
        ttl_seconds: Duree de vie du verrou (doit depasser le timeout du job
            appelant, pour ne jamais expirer pendant qu'une sync est en cours).

    Returns:
        True si le verrou a ete acquis, False si une sync est deja en cours.
    """
    key = f"{_LOCK_KEY_PREFIX}{connection_id}"
    result = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    return bool(result)


async def release_sync_lock(redis, connection_id: int) -> None:
    """Libere le verrou de synchronisation d'une connexion.

    Args:
        redis: Client Redis partage.
        connection_id: Identifiant de connexion POS dont le verrou doit etre libere.
    """
    key = f"{_LOCK_KEY_PREFIX}{connection_id}"
    await redis.delete(key)
