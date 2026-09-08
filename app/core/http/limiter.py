"""Singleton SlowAPI limiter partagé entre main.py et les routers.

Ce module évite l'import circulaire : main.py importe les routers,
donc si les routers importaient limiter depuis main.py on aurait un cycle.

[🔒 SÉCURITÉ] Stockage Redis partagé (``settings.redis_url`` -- le même Redis
que le pub/sub WebSocket et les flags ``ws:ip_banned``/``ws:ip_attempts``, voir
``app.modules.notifications.ws_router``) : les compteurs de limite sont donc
cohérents entre toutes les instances API Railway, pas seulement locaux au
process qui a reçu la requête. Sans ça, un attaquant peut répartir ses
requêtes entre plusieurs instances pour multiplier une limite censée être
globale par le nombre d'instances déployées.

[⚠️ PROD] Comportement dégradé si Redis est indisponible (décision
documentée, pas un oubli) : ``in_memory_fallback`` bascule TOUTES les routes
sur une limite de secours unique et conservative (``_FALLBACK_LIMIT``),
appliquée en mémoire LOCALE à l'instance (donc de nouveau non partagée entre
instances tant que Redis reste indisponible) -- voir slowapi/limits
(``Limiter._storage_dead``/``__should_check_backend``) qui retente
periodiquement (backoff exponentiel) la connexion Redis et repasse
automatiquement sur le stockage partagé dès qu'il répond de nouveau. Ce choix
privilégie la disponibilité de l'API (jamais de 500 générique sur toutes les
routes limitées le temps d'une coupure Redis) sur la précision par route :
pendant la coupure, même une route normalement plus permissive (ex: catalogue
public à 60/minute) tombe sur cette limite de secours, généralement plus
stricte -- accepté car une coupure Redis est déjà signalée comme dégradation
par ``GET /health/ready`` (voir app.main), qui retire l'instance du load
balancer Railway.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings

# Limite de secours globale (IP uniquement -- ``in_memory_fallback`` utilise
# toujours la key_func par défaut du Limiter, jamais celle d'une route
# individuelle) appliquée à TOUTES les routes limitées quand Redis est
# indisponible. Volontairement plus stricte que la route publique la plus
# permissive (catalogue, 60/minute) pour rester protectrice sur les routes
# sensibles (login, MFA...) qui perdent temporairement leur limite dédiée.
_FALLBACK_LIMIT = "20/minute"

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
    in_memory_fallback=[_FALLBACK_LIMIT],
)


def user_or_ip_key(request: Request) -> str:
    """Clé de rate limiting par identité utilisateur si authentifié, IP sinon.

    [🔒 SÉCURITÉ] Utilisée pour les routes déjà authentifiées (MFA, imports/
    exports admin) où limiter par IP seule permettrait à plusieurs comptes
    derrière la même IP (NAT, proxy d'entreprise) de s'épuiser mutuellement
    leur quota, et où limiter par utilisateur seul permettrait à un même
    utilisateur de contourner la limite en changeant d'IP. Lit
    ``request.state.user_id``/``tenant_slug``, peuplés par
    ``TenantMiddleware`` (décodage JWT) avant que ce hook ne s'exécute --
    jamais un nouveau décodage ici. Retombe sur l'IP si non authentifié
    (ne devrait pas arriver derrière une dépendance ``require_role``/
    ``require_permission``, mais reste sûr en filet).

    Args:
        request: Requête Starlette en cours.

    Returns:
        ``"user:{tenant_slug}:{user_id}"`` si authentifié, ``"ip:{addr}"`` sinon.
    """
    user_id = getattr(request.state, "user_id", None)
    tenant_slug = getattr(request.state, "tenant_slug", None)
    if user_id:
        return f"user:{tenant_slug}:{user_id}"
    return f"ip:{get_remote_address(request)}"
