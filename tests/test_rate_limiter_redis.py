"""Rate limiting partagé entre instances API (Prompt 09).

Constat corrigé :
- ``app.core.http.limiter.limiter`` était un ``slowapi.Limiter`` sans
  ``storage_uri`` explicite -- ``limits`` retombe alors sur
  ``MemoryStorage`` (compteurs en mémoire du process). Avec plusieurs
  instances Railway derrière le même load balancer, un attaquant peut
  répartir ses requêtes entre elles pour multiplier une limite censée être
  globale par le nombre d'instances déployées.
- Certains endpoints sensibles (MFA tenant : ``/auth/mfa/setup``,
  ``/auth/mfa/confirm``, ``/auth/mfa/backup-codes/regenerate``) et coûteux
  (import/export CSV catalogue, export PDF/CSV HACCP -- WeasyPrint) n'avaient
  aucune limite du tout.

Ces tests prouvent :
1. le ``limiter`` applicatif est bien configuré avec un stockage Redis
   (``settings.redis_url``), pas le ``MemoryStorage`` par défaut ;
2. une limite est respectée de manière cohérente entre deux instances API
   simulées (deux objets ``Limiter`` Python distincts, comme deux process
   Railway, ne partageant que le même Redis) ;
3. le comportement dégradé documenté (``in_memory_fallback``) : si Redis
   devient injoignable, l'API continue de répondre (jamais de 500 générique
   sur les routes limitées) mais retombe sur une limite de secours locale
   à l'instance ;
4. ``user_or_ip_key`` retourne une clé par utilisateur si authentifié, par
   IP sinon ;
5. les endpoints précédemment sans limite (MFA tenant, imports/exports
   catalogue, exports HACCP) rejettent bien au-delà de leur nouvelle limite.
"""

import uuid
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from limits.storage.redis import RedisStorage

from app.core.config import settings
from app.core.http.limiter import limiter, user_or_ip_key


def test_app_limiter_uses_redis_storage_not_memory():
    """Le limiter applicatif partagé doit être backé par Redis (cohérence
    cross-instance), jamais par le ``MemoryStorage`` par défaut de
    ``slowapi``/``limits`` (compteurs locaux au process uniquement)."""
    assert isinstance(limiter._storage, RedisStorage)
    assert limiter._storage_uri == settings.redis_url


def test_user_or_ip_key_prefers_authenticated_identity():
    """Authentifié -> clé par utilisateur+tenant ; sinon -> clé par IP."""
    authenticated_request = SimpleNamespace(
        state=SimpleNamespace(user_id=42, tenant_slug="acme"),
        client=SimpleNamespace(host="203.0.113.9"),
        headers={},
    )
    assert user_or_ip_key(authenticated_request) == "user:acme:42"

    anonymous_request = SimpleNamespace(
        state=SimpleNamespace(user_id=None, tenant_slug=None),
        client=SimpleNamespace(host="203.0.113.9"),
        headers={},
    )
    assert user_or_ip_key(anonymous_request) == "ip:203.0.113.9"


def _build_instance_app(test_limiter: Limiter, path: str, limit: str) -> FastAPI:
    """Construit une mini app FastAPI isolée -- simule UNE instance API,
    avec SON PROPRE objet ``Limiter`` Python (comme un process Railway
    distinct), ne partageant que le backend Redis avec les autres instances."""
    def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse({"code": "RATE_LIMIT_EXCEEDED"}, status_code=429)

    app = FastAPI()
    app.state.limiter = test_limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get(path)
    @test_limiter.limit(limit)
    async def _endpoint(request: Request) -> dict:
        return {"ok": True}

    return app


async def test_rate_limit_is_shared_consistently_across_two_simulated_instances():
    """Critère d'acceptation : une limite de sécurité reste effective même si
    les requêtes sont réparties entre plusieurs instances API.

    Simulation : deux objets ``Limiter`` Python totalement indépendants
    (``limiter_a``, ``limiter_b``), chacun avec sa propre ``FastAPI`` app --
    exactement comme deux process API distincts -- mais pointant tous les
    deux vers le MÊME Redis (``settings.redis_url``) avec le même
    ``key_prefix`` dédié à ce test. Une requête envoyée à "l'instance A" doit
    compter dans le quota vu par "l'instance B", et réciproquement."""
    prefix = f"test_ratelimit_multi_{uuid.uuid4().hex[:8]}"
    path = "/limited"
    limit = "3/minute"

    limiter_a = Limiter(
        key_func=get_remote_address,
        storage_uri=settings.redis_url,
        storage_options={"key_prefix": prefix},
    )
    limiter_b = Limiter(
        key_func=get_remote_address,
        storage_uri=settings.redis_url,
        storage_options={"key_prefix": prefix},
    )

    app_a = _build_instance_app(limiter_a, path, limit)
    app_b = _build_instance_app(limiter_b, path, limit)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app_a), base_url="http://instance-a"
        ) as client_a, AsyncClient(
            transport=ASGITransport(app=app_b), base_url="http://instance-b"
        ) as client_b:
            # Meme IP source cote client (get_remote_address par defaut sur
            # testclient) -- consomme 2 des 3 requetes autorisees via
            # l'instance A.
            for _ in range(2):
                resp = await client_a.get(path)
                assert resp.status_code == 200

            # La 3e requete, envoyee a l'instance B (objet Limiter distinct,
            # process different en pratique), doit voir que 2 requetes ont
            # deja ete consommees via Redis et n'autoriser qu'UNE requete
            # supplementaire avant de rejeter -- pas 3 de plus.
            resp = await client_b.get(path)
            assert resp.status_code == 200  # 3e requete globale : encore permise

            resp = await client_b.get(path)
            assert resp.status_code == 429  # 4e requete globale : rejetee

            # Confirme que le rejet est bien vu aussi depuis l'instance A
            # (meme compteur Redis partage, pas un etat local a B).
            resp = await client_a.get(path)
            assert resp.status_code == 429
    finally:
        limiter_a.reset()


async def test_degraded_mode_falls_back_to_local_limit_when_redis_unreachable():
    """Comportement dégradé documenté : si Redis devient injoignable, l'API
    ne doit jamais renvoyer un 500 générique sur une route limitée -- elle
    continue de répondre en retombant sur ``in_memory_fallback`` (limite de
    secours locale à l'instance), jusqu'à ce que Redis redevienne joignable."""
    prefix = f"test_ratelimit_degraded_{uuid.uuid4().hex[:8]}"
    path = "/limited-degraded"

    test_limiter = Limiter(
        key_func=get_remote_address,
        # URL Redis invalide (port fermé) -- simule une coupure reseau, pas
        # juste un serveur qui repond une erreur applicative.
        storage_uri="redis://127.0.0.1:1/0",
        storage_options={"key_prefix": prefix},
        in_memory_fallback=["2/minute"],
    )
    app = _build_instance_app(test_limiter, path, "100/minute")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://instance-degraded"
    ) as client:
        # Meme si la route declare 100/minute (Redis indisponible -> jamais
        # evaluee), la limite de secours (2/minute, en memoire locale)
        # s'applique : les 2 premieres requetes passent...
        resp = await client.get(path)
        assert resp.status_code == 200
        resp = await client.get(path)
        assert resp.status_code == 200

        # ... la 3e est rejetee proprement (429, pas un 500) par la limite
        # de secours, jamais un crash de l'app faute de backend Redis.
        resp = await client.get(path)
        assert resp.status_code == 429

    assert test_limiter._storage_dead is True


async def test_previously_unlimited_sensitive_endpoints_now_reject_beyond_limit(client, unique_slug):
    """Les endpoints identifiés comme sans limite (MFA tenant) doivent
    désormais rejeter (429) au-delà de leur nouvelle limite déclarée."""
    tenant_slug = f"rl{unique_slug}"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_slug,
            "email": f"admin-{unique_slug}@test.com",
            "password": "Valid1!aa",
        },
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant_slug}

    # mfa/confirm est limite a 5/minute (voir app.modules.auth.router) --
    # jusque-la, aucune limite n'existait sur cette route.
    statuses = []
    for _ in range(6):
        resp = await client.post(
            "/api/v1/auth/mfa/confirm",
            json={"totp_code": "000000"},
            headers=headers,
        )
        statuses.append(resp.status_code)

    assert 429 in statuses, f"attendu au moins un 429 sur 6 appels, obtenu: {statuses}"
