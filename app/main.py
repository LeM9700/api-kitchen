import asyncio
import time
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from slowapi.errors import RateLimitExceeded

from app.core.services.cloudinary import init_cloudinary
from app.core.config import settings
from app.core.http.errors import AppError, app_error_handler
from app.core.http.limiter import limiter
from app.core.http.security_headers import SecurityHeadersMiddleware
from app.core.tenancy.tenant import TenantMiddleware
from worker.main import get_redis_settings

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gere le cycle de vie de l'application : initialise et ferme les singletons partages.

    Singletons exposes via app.state :
    - motor_client : instance AsyncIOMotorClient reutilisee par toutes les routes admin.
    - arq_pool : pool de connexions Redis arq reutilise pour l'enqueue des jobs.

    Args:
        app: Instance FastAPI en cours de demarrage.

    Yields:
        Controle a l'application durant sa duree de vie.
    """
    # --- startup ---
    init_cloudinary(settings)
    app.state.motor_client = AsyncIOMotorClient(settings.mongo_url)
    app.state.arq_pool = await create_pool(get_redis_settings())

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
    try:
        await app.state.arq_pool.close()
        await app.state.arq_pool.wait_closed()
    except Exception:
        pass
    try:
        app.state.motor_client.close()
    except Exception:
        pass


class _RequestSizeLimitMiddleware:
    """Middleware ASGI qui rejette les requetes dont le Content-Length depasse MAX_BYTES.

    [🔒 SÉCURITÉ] Sans cette limite, un attaquant peut envoyer des payloads
    massifs (plusieurs centaines de Mo) qui saturent la RAM du processus uvicorn.
    FastAPI lit le body complet avant la validation Pydantic — il n'y a pas de
    protection native.

    La limite de 1 Mo s'applique a toutes les routes. Les images (8 Mo max) sont
    validees au niveau de Cloudinary service apres le check de taille ici — les
    uploads d'images ne depassent pas 1 Mo avant multipart parsing si l'app mobile
    compresse correctement.

    Note : seul l'en-tete Content-Length est verifie ici (protection rapide, pas
    de lecture du body). Un client malicieux peut omettre Content-Length — la
    protection complementaire est le timeout uvicorn (--timeout-keep-alive).
    """

    MAX_BYTES: int = 1 * 1024 * 1024       # 1 Mo pour les routes JSON
    MAX_BYTES_UPLOAD: int = 10 * 1024 * 1024  # 10 Mo pour les routes d'upload (Cloudinary valide à 8 Mo)

    # Préfixes de routes d'upload multipart — limite étendue à MAX_BYTES_UPLOAD.
    _UPLOAD_PREFIXES: tuple[str, ...] = (
        b"/api/v1/catalog/",  # images produits/catégories/extras/variantes
    )

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            content_length = headers.get(b"content-length")
            if content_length:
                try:
                    path: bytes = scope.get("path", b"").encode() if isinstance(scope.get("path"), str) else scope.get("path", b"")
                    is_upload = any(path.startswith(p) for p in self._UPLOAD_PREFIXES)
                    limit = self.MAX_BYTES_UPLOAD if is_upload else self.MAX_BYTES
                    if int(content_length) > limit:
                        response = JSONResponse(
                            {
                                "code": "PAYLOAD_TOO_LARGE",
                                "detail": f"Body trop volumineux (max {limit // (1024 * 1024)} Mo).",
                            },
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                except (ValueError, TypeError):
                    pass
        await self.app(scope, receive, send)


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
        # [🔒 SÉCURITÉ] Swagger et ReDoc désactivés en production pour ne pas
        # exposer la surface d'attaque complète de l'API.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_exception_handler(AppError, app_error_handler)

    # Ordre d'enregistrement = ordre LIFO d'execution dans Starlette.
    # CORSMiddleware en premier (dernier dans la chaine) pour traiter les preflight avant tout.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Slug", "stripe-signature", "Idempotency-Key"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TenantMiddleware)
    # [🔒 SÉCURITÉ] Limite la taille des requêtes HTTP à 1 Mo.
    # Doit être en dernier dans add_middleware (premier exécuté en LIFO Starlette).
    app.add_middleware(_RequestSizeLimitMiddleware)

    from app.modules.auth.router import router as auth_router
    from app.modules.catalog.allergen.allergen_router import router as allergen_router
    from app.modules.catalog.image.image_router import router as image_router
    from app.modules.catalog.router import router as catalog_router
    from app.modules.notifications.router import router as notifications_router
    from app.modules.notifications.ws_router import router as notifications_ws_router
    from app.modules.orders.router import router as orders_router
    from app.modules.payments.router import router as payments_router
    from app.modules.payments.connect_router import router as payments_connect_router
    from app.modules.stock.router import router as stock_router
    from app.modules.loyalty.router import router as loyalty_router
    from app.modules.promotions.router import router as promotions_router
    from app.modules.delivery.router import router as delivery_router
    from app.modules.admin.router import router as admin_router
    from app.modules.admin.tenants.router import router as tenant_router
    from app.modules.customer.router import router as customer_router

    prefix = "/api/v1"
    app.include_router(auth_router, prefix=prefix + "/auth", tags=["auth"])
    app.include_router(catalog_router, prefix=prefix + "/catalog", tags=["catalog"])
    app.include_router(allergen_router, prefix=prefix + "/catalog", tags=["catalog-allergens"])
    app.include_router(image_router, prefix=prefix + "/catalog", tags=["catalog-images"])
    app.include_router(orders_router, prefix=prefix + "/orders", tags=["orders"])
    app.include_router(payments_router, prefix=prefix + "/payments", tags=["payments"])
    app.include_router(payments_connect_router, prefix=prefix + "/payments/connect", tags=["payments-connect"])
    app.include_router(stock_router, prefix=prefix + "/stock", tags=["stock"])
    app.include_router(loyalty_router, prefix=prefix + "/loyalty")
    app.include_router(promotions_router, prefix=prefix + "/promotions", tags=["promotions"])
    app.include_router(delivery_router, prefix=prefix + "/delivery", tags=["delivery"])
    app.include_router(admin_router, prefix=prefix + "/admin")
    app.include_router(tenant_router, prefix=prefix + "/tenant", tags=["tenant"])
    app.include_router(notifications_router, prefix=prefix + "/notifications", tags=["notifications"])
    app.include_router(notifications_ws_router, prefix=prefix, tags=["notifications-ws"])
    app.include_router(customer_router, prefix=prefix + "/customer", tags=["customer"])

    # ---------------------------------------------------------------------------
    # Health check — utilisé par Railway pour les liveness/readiness probes.
    # Pas de dépendance sur la BDD : si l'app répond, le process est vivant.
    # Pour une readiness probe DB, ajouter un endpoint /health/ready séparé.
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

    return app


app = create_app()
