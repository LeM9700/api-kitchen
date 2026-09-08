import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import sentry_sdk
from arq import create_pool
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.core.services.cloudinary import init_cloudinary
from app.core.config import settings
from app.core.database.session import engine
from app.core.http.errors import AppError, app_error_handler
from app.core.http.limiter import limiter
from app.core.http.logging_config import configure_logging, set_request_id
from app.core.http.request_size_limit import RequestSizeLimitMiddleware
from app.core.http.security_headers import SecurityHeadersMiddleware
from app.core.tenancy.tenant import TenantMiddleware
from app.modules.notifications import ws_router
from app.modules.payments import service as payments_service
from worker.main import get_redis_settings

configure_logging()
logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.1,
    )

# [🔒 SÉCURITÉ] Import déclenche l'enregistrement du listener SQLAlchemy qui
# bloque la publication d'un produit sans allergènes réglementaires complets.
import app.modules.catalog.allergen.allergen_events  # noqa: F401


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Custom handler for rate limit errors that includes Retry-After header."""
    return JSONResponse(
        status_code=429,
        content={"code": "RATE_LIMIT_EXCEEDED", "detail": str(exc.detail)},
        headers={"Retry-After": "60"},
    )


def _database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a stable 503 payload when database connectivity is degraded."""
    logger.exception("database unavailable", extra={"path": request.url.path})
    return JSONResponse(
        status_code=503,
        content={
            "code": "DATABASE_UNAVAILABLE",
            "detail": "Database temporarily unavailable. Please retry.",
            "field": None,
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gere le cycle de vie de l'application : initialise et ferme les singletons partages.

    Singletons exposes via app.state :
    - motor_client : instance AsyncIOMotorClient reutilisee par toutes les routes admin.
    - arq_pool : pool de connexions Redis arq reutilise pour l'enqueue des jobs.
    - ws_redis_subscriber_task : tache background unique qui ecoute notif:*/
      session_revoked:* et alimente les WebSockets de notifications (voir
      app.modules.notifications.ws_router._redis_subscriber) -- sans ce
      demarrage, une WebSocket ouverte ne recoit ni les notifications
      applicatives ni le signal de fermeture immediate suite a une
      desactivation/revocation/retrait de permissions.

    Args:
        app: Instance FastAPI en cours de demarrage.

    Yields:
        Controle a l'application durant sa duree de vie.
    """
    # --- startup ---
    payments_service.warn_if_webhook_connect_secret_missing()
    init_cloudinary(settings)
    app.state.motor_client = AsyncIOMotorClient(settings.mongo_url)
    app.state.arq_pool = await create_pool(get_redis_settings())
    # [🔒 SÉCURITÉ] Une seule tache par process API : le lifespan FastAPI
    # n'entre qu'une fois par process (pas de re-entree sur un meme `app`
    # deja demarre), donc pas de garde supplementaire necessaire ici contre
    # un double-demarrage -- voir _redis_subscriber pour le detail.
    app.state.ws_redis_subscriber_task = asyncio.create_task(ws_router._redis_subscriber())

    # Ensure TTL index (90 days) on all existing login_events_* collections.
    # Non-blocking — does not delay startup.
    async def _ensure_login_events_ttl_index() -> None:
        try:
            db = app.state.motor_client[settings.mongo_db]
            collection_names = await db.list_collection_names()
            for name in collection_names:
                if name.startswith("login_events_"):
                    await db[name].create_index(
                        [("created_at", 1)],
                        expireAfterSeconds=90 * 24 * 3600,
                        background=True,
                    )
        except Exception:
            pass

    asyncio.create_task(_ensure_login_events_ttl_index())

    yield

    # --- shutdown ---
    app.state.ws_redis_subscriber_task.cancel()
    try:
        await app.state.ws_redis_subscriber_task
    except asyncio.CancelledError:
        pass
    try:
        await app.state.arq_pool.close()
        await app.state.arq_pool.wait_closed()
    except Exception:
        pass
    try:
        app.state.motor_client.close()
    except Exception:
        pass


def create_app() -> FastAPI:
    """Construit et configure l'instance FastAPI avec tous les routers et middlewares.

    Returns:
        Instance FastAPI prete a etre servie par uvicorn.
    """
    is_production = (settings.environment or "").lower() == "production"

    app = FastAPI(
        title="Pizzeria API",
        version="1.0.0",
        lifespan=lifespan,
        # [🔒 SÉCURITÉ] Décision explicite et documentée (pas un oubli) : en
        # production, Swagger UI, ReDoc ET le schéma OpenAPI brut
        # (``/openapi.json``, servi par défaut par FastAPI même quand
        # docs_url/redoc_url sont désactivés -- c'était le trou avant ce
        # commit) sont tous les trois désactivés.
        #
        # Ceci réduit l'EXPOSITION DOCUMENTAIRE (la liste structurée et
        # exhaustive de toutes les routes, schémas de requête/réponse, et
        # noms de champs, prête à l'emploi pour un reconnaissance
        # automatisée) mais n'est PAS un contrôle de sécurité suffisant en
        # soi : un attaquant déterminé retrouve la même information par
        # d'autres moyens (code source si le repo fuite, réponses d'erreur
        # 422 de validation Pydantic qui révèlent les noms de champs
        # attendus, énumération manuelle des routes). L'authentification,
        # l'autorisation et la validation métier sur chaque route restent
        # les VRAIS contrôles -- voir ``app.core.http.deps``,
        # ``app.core.tenancy.tenant`` -- jamais l'absence de ce schéma.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(TimeoutError, _database_unavailable_handler)
    app.add_exception_handler(SQLAlchemyTimeoutError, _database_unavailable_handler)

    # Ordre d'enregistrement = ordre LIFO d'execution dans Starlette : le
    # DERNIER add_middleware() devient le PLUS EXTERIEUR (il voit la requete
    # en premier, la reponse en dernier).
    #
    # CORSMiddleware doit etre le plus exterieur de tous. SecurityHeaders,
    # Tenant et RequestSizeLimit peuvent chacun court-circuiter la requete
    # (return direct sans call_next : 403 must_change_password, 413 payload
    # trop gros...). Si CORSMiddleware est plus interieur qu'eux, ces
    # reponses n'ont jamais l'en-tete Access-Control-Allow-Origin : le
    # navigateur les bloque comme une violation CORS opaque au lieu de
    # laisser le client lire le vrai code d'erreur (ex: le flux
    # "mot de passe a changer" devient invisible cote frontend et retombe
    # sur l'ecran de connexion au lieu de l'ecran dedie).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TenantMiddleware)
    # [🔒 SÉCURITÉ] Limite la taille des requêtes HTTP, appliquée au flux reçu
    # (pas seulement à Content-Length) -- voir app.core.http.request_size_limit.
    app.add_middleware(RequestSizeLimitMiddleware)
    local_cors_regex = (
        r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?$"
        if not is_production
        else None
    )
    # Doit rester le DERNIER add_middleware() de ce bloc (le plus exterieur).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=local_cors_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Tenant-Slug",
            "stripe-signature",
            "Idempotency-Key",
            "X-Request-ID",
            "X-KDS-Session",
        ],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def _log_request_duration(request: Request, call_next):
        """Log la durée de chaque requête en JSON structuré avec un request_id de
        corrélation — complète les traces Sentry pour le débogage manuel de logs bruts.

        [PROD] Le request_id est repris depuis le header entrant ``X-Request-ID``
        s'il est fourni (ex: propagé par un proxy/CDN), sinon généré. Il est
        renvoyé sur la réponse pour permettre au client de le référencer dans un
        rapport d'incident, et attaché au contexte de logging (voir
        ``logging_config.py``) pour que tous les logs émis pendant cette requête
        — pas seulement cette ligne — le portent automatiquement.
        """
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        set_request_id(request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    from app.modules.auth.router import router as auth_router
    from app.modules.catalog.allergen.allergen_router import router as allergen_router
    from app.modules.catalog.image.image_router import router as image_router
    from app.modules.catalog.router import router as catalog_router
    from app.modules.favorites.router import router as favorites_router
    from app.modules.notifications.router import router as notifications_router
    from app.modules.notifications.ws_router import router as notifications_ws_router
    from app.modules.orders.router import router as orders_router
    from app.modules.payments.router import router as payments_router
    from app.modules.payments.connect_router import router as payments_connect_router
    from app.modules.pos.router import router as pos_router
    from app.modules.pos.router import webhook_router as pos_webhook_router
    from app.modules.stock.router import router as stock_router
    from app.modules.loyalty.router import router as loyalty_router
    from app.modules.promotions.router import router as promotions_router
    from app.modules.delivery.router import router as delivery_router
    from app.modules.hr.router import router as hr_router
    from app.modules.kds.router import router as kds_router
    from app.modules.admin.router import router as admin_router
    from app.modules.admin.tenants.router import router as tenant_router
    from app.modules.customer.router import router as customer_router
    from app.modules.super_admin.router import router as super_admin_router
    from app.modules.haccp.router import router as haccp_router
    from app.modules.haccp.export_router import router as haccp_export_router
    from app.modules.haccp.stats_router import router as haccp_stats_router

    prefix = "/api/v1"
    app.include_router(auth_router, prefix=prefix + "/auth", tags=["auth"])
    app.include_router(catalog_router, prefix=prefix + "/catalog", tags=["catalog"])
    app.include_router(allergen_router, prefix=prefix + "/catalog", tags=["catalog-allergens"])
    app.include_router(image_router, prefix=prefix + "/catalog", tags=["catalog-images"])
    app.include_router(orders_router, prefix=prefix + "/orders", tags=["orders"])
    app.include_router(payments_router, prefix=prefix + "/payments", tags=["payments"])
    app.include_router(payments_connect_router, prefix=prefix + "/payments/connect", tags=["payments-connect"])
    app.include_router(pos_router, prefix=prefix + "/pos/connect", tags=["pos-connect"])
    app.include_router(pos_webhook_router, prefix=prefix + "/pos", tags=["pos-webhook"])
    app.include_router(stock_router, prefix=prefix + "/stock", tags=["stock"])
    app.include_router(haccp_router, prefix=prefix + "/haccp", tags=["haccp"])
    app.include_router(haccp_export_router, prefix=prefix + "/haccp", tags=["haccp-export"])
    app.include_router(haccp_stats_router, prefix=prefix + "/haccp", tags=["haccp-stats"])
    app.include_router(loyalty_router, prefix=prefix + "/loyalty")
    app.include_router(promotions_router, prefix=prefix + "/promotions", tags=["promotions"])
    app.include_router(delivery_router, prefix=prefix + "/delivery", tags=["delivery"])
    app.include_router(hr_router, prefix=prefix + "/hr", tags=["hr"])
    app.include_router(kds_router, prefix=prefix + "/kds", tags=["kds"])
    app.include_router(admin_router, prefix=prefix + "/admin")
    app.include_router(tenant_router, prefix=prefix + "/tenant", tags=["tenant"])
    app.include_router(notifications_router, prefix=prefix + "/notifications", tags=["notifications"])
    app.include_router(notifications_ws_router, prefix=prefix, tags=["notifications-ws"])
    app.include_router(customer_router, prefix=prefix + "/customer", tags=["customer"])
    app.include_router(favorites_router, prefix=prefix + "/favorites", tags=["favorites"])
    app.include_router(super_admin_router, prefix=prefix + "/super-admin", tags=["super-admin"])

    # ---------------------------------------------------------------------------
    # Health check — utilisé par Railway pour la liveness probe.
    # Pas de dépendance sur la BDD : si l'app répond, le process est vivant.
    # La readiness probe (DB + Redis) est exposée séparément sur /health/ready.
    # ---------------------------------------------------------------------------
    _startup_time = time.time()

    @app.get("/health", tags=["ops"], include_in_schema=not is_production)
    async def health() -> dict:
        """Liveness probe — Railway arrête le container si ce endpoint ne répond pas.

        Returns:
            Dictionnaire avec le statut, la version et l'uptime en secondes.
        """
        return {
            "status": "ok",
            "version": app.version,
            "uptime_seconds": round(time.time() - _startup_time),
            "environment": settings.environment,
        }

    @app.get("/health/ready", tags=["ops"], include_in_schema=not is_production)
    async def health_ready() -> JSONResponse:
        """Readiness probe — vérifie que la DB et Redis répondent avant de recevoir du trafic.

        Retourne 503 si l'une des dépendances est indisponible, pour que Railway
        retire l'instance du load balancer plutôt que de lui envoyer du trafic
        qui échouera systématiquement.
        """
        checks = {"database": False, "redis": False}

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            logger.exception("health/ready: database check failed")

        try:
            from redis.asyncio import from_url as redis_from_url

            redis_client = redis_from_url(settings.redis_url)
            try:
                await redis_client.ping()
                checks["redis"] = True
            finally:
                await redis_client.close()
        except Exception:
            logger.exception("health/ready: redis check failed")

        ok = all(checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "unavailable", "checks": checks},
        )

    return app


app = create_app()
