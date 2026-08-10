"""Service de connexion OAuth 2.0 a un hub POS externe.

Flux :
    1. POST /pos/connect/start -- genere un state a usage unique (stocke
       dans Redis, TTL 10 min), retourne l'URL d'autorisation du hub.
    2. Le restaurant autorise sur le site du hub POS.
    3. GET /pos/connect/callback -- consomme le state, echange le code
       contre des tokens, chiffre et persiste la connexion.
    4. POST /pos/connect/disconnect -- revoque la connexion (best-effort
       cote fournisseur) et repasse le tenant en mode standalone.

[SECURITE]
    - Le state est a usage unique : consume_oauth_state fait un GETDEL
      atomique (lecture + suppression en un seul aller-retour Redis), donc
      un rejeu echoue toujours (la cle n'existe plus).
    - Les tokens sont chiffres (app.core.services.crypto) avant toute
      ecriture en base, et jamais loggues.
"""
import secrets

from app.core.http.errors import AppError

STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "pos_oauth_state:"


def generate_state() -> str:
    """Genere un identifiant de state OAuth imprevisible.

    Returns:
        Chaine urlsafe de 43 caracteres (32 octets d'entropie).
    """
    return secrets.token_urlsafe(32)


async def store_oauth_state(redis, state: str, tenant_slug: str) -> None:
    """Associe un state OAuth au tenant qui a initie le flux, avec TTL.

    Args:
        redis: Pool Redis partage (ArqRedis, app.state.arq_pool).
        state: Identifiant genere par generate_state().
        tenant_slug: Slug du tenant qui a initie le flux.
    """
    await redis.setex(f"{_STATE_KEY_PREFIX}{state}", STATE_TTL_SECONDS, tenant_slug)


async def consume_oauth_state(redis, state: str) -> str:
    """Consomme un state OAuth a usage unique et retourne le tenant associe.

    Args:
        redis: Pool Redis partage.
        state: State recu sur le callback OAuth.

    Returns:
        Slug du tenant qui avait initie le flux.

    Raises:
        AppError: POS_OAUTH_INVALID_STATE (400) si le state est absent,
            expire ou deja consomme -- ces trois cas sont indistinguables
            volontairement (pas d'information donnee a un attaquant).
    """
    value = await redis.getdel(f"{_STATE_KEY_PREFIX}{state}")
    if value is None:
        raise AppError(
            "POS_OAUTH_INVALID_STATE",
            "State OAuth invalide, expire ou deja utilise.",
            400,
        )
    return value.decode() if isinstance(value, bytes) else value
