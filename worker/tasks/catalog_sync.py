"""Task ARQ : synchronise le catalogue d'une connexion POS depuis le hub vers
catalog_snapshots (schema tenant). Jamais appelee pendant une requete entrante
-- uniquement depuis le webhook /pos/catalog-webhook, le cron de securite
(sync_stale_catalog_connections), ou une resynchronisation planifiee par
HubCatalogProvider sur un snapshot perime.
"""
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.catalog import snapshot_repository
from app.modules.catalog.hub_client import HttpHubCatalogClient
from app.modules.catalog.normalize import normalize_catalog
from app.modules.catalog.sync_guards import acquire_sync_lock, check_rate_limit, release_sync_lock
from worker.tasks.worker_utils import with_dead_letter

logger = logging.getLogger(__name__)

# Superieur au job_timeout global (120s, WorkerSettings) -- le verrou ne doit
# jamais expirer pendant qu'une synchronisation legitime est en cours.
_SYNC_LOCK_TTL_SECONDS = 150


def _session_factory():
    engine = create_async_engine(settings.database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _load_active_connection(session, connection_id: int) -> dict | None:
    result = await session.execute(
        text(
            "SELECT pc.id, pc.access_token_encrypted, t.slug AS tenant_slug "
            "FROM public.pos_connections pc "
            "JOIN public.tenants t ON t.id = pc.tenant_id "
            "WHERE pc.id = :connection_id AND pc.status = 'active'"
        ),
        {"connection_id": connection_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


@with_dead_letter
async def sync_catalog_from_hub(ctx, connection_id: int) -> None:
    """Recupere le catalogue du hub pour une connexion POS et met a jour son snapshot.

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``, ``job_try``).
        connection_id: Identifiant ``public.pos_connections.id`` a synchroniser.
    """
    session_factory = _session_factory()
    redis = ctx.get("redis")

    session = session_factory()
    try:
        connection = await _load_active_connection(session, connection_id)
    finally:
        await session.close()

    if connection is None:
        logger.warning("sync_catalog_from_hub: connection_id=%s introuvable ou inactive", connection_id)
        return

    if redis is not None:
        locked = await acquire_sync_lock(redis, connection_id, _SYNC_LOCK_TTL_SECONDS)
        if not locked:
            logger.info("sync_catalog_from_hub: sync deja en cours, connection_id=%s", connection_id)
            return

    try:
        if redis is not None:
            allowed = await check_rate_limit(redis, connection_id, settings.pos_hub_catalog_rate_limit_per_minute)
            if not allowed:
                logger.info("sync_catalog_from_hub: rate limit atteint, re-enqueue connection_id=%s", connection_id)
                await redis.enqueue_job("sync_catalog_from_hub", connection_id=connection_id, _defer_by=30)
                return

        try:
            client = HttpHubCatalogClient()
            payload = await client.fetch_catalog(connection)
            normalized = normalize_catalog(payload)
        except Exception as exc:
            logger.error(
                "sync_catalog_from_hub: echec recuperation/normalisation connection_id=%s error_type=%s",
                connection_id,
                type(exc).__name__,
            )
            raise

        schema = tenant_schema_name(connection["tenant_slug"])
        tenant_session = session_factory()
        try:
            await tenant_session.execute(text(f'SET search_path TO "{schema}", public'))
            await snapshot_repository.upsert_snapshot(tenant_session, connection_id, payload, normalized)
        finally:
            await tenant_session.close()

        logger.info("sync_catalog_from_hub: succes connection_id=%s produits=%s", connection_id, len(normalized))
    finally:
        if redis is not None:
            await release_sync_lock(redis, connection_id)
