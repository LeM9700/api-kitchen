"""Task ARQ : synchronise le catalogue d'une connexion POS depuis le hub vers
catalog_snapshots (schema tenant). Jamais appelee pendant une requete entrante
-- uniquement depuis le webhook /pos/catalog-webhook, le cron de securite
(sync_stale_catalog_connections), ou une resynchronisation planifiee par
HubCatalogProvider sur un snapshot perime.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.catalog import hub_client, snapshot_repository
from app.modules.catalog.hub_client import HttpHubCatalogClient
from app.modules.catalog.normalize import normalize_catalog
from app.modules.catalog.sync_guards import acquire_sync_lock, check_rate_limit, release_sync_lock
from worker.tasks.worker_utils import with_dead_letter

logger = logging.getLogger(__name__)

# Superieur au job_timeout global (120s, WorkerSettings) -- le verrou ne doit
# jamais expirer pendant qu'une synchronisation legitime est en cours.
_SYNC_LOCK_TTL_SECONDS = 150


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
    # Engine cree directement ici (comme worker/tasks/stock_alerts.py::send_stock_alert)
    # et dispose() dans le finally externe -- ce worker etant appele par job (webhook,
    # cron de balayage), un engine non dispose fuirait une pool de connexions Postgres
    # par invocation.
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = ctx.get("redis")

    try:
        session = session_factory()
        try:
            connection = await _load_active_connection(session, connection_id)
        finally:
            await session.close()

        if connection is None:
            logger.warning("sync_catalog_from_hub: connection_id=%s introuvable ou inactive", connection_id)
            return

        # Garde-fou global (Contraintes globales du plan) : pos_hub_catalog_url vide
        # = fonctionnalite desactivee. Verifie AVANT le verrou/rate-limiter/appel hub
        # -- sans ce garde-fou, toute connexion active mais non configuree serait
        # re-tentee (et mise en dead-letter) a chaque cron horaire, indefiniment.
        if not hub_client.is_configured():
            logger.info(
                "sync_catalog_from_hub: hub non configure (pos_hub_catalog_url vide), sync ignoree connection_id=%s",
                connection_id,
            )
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
                # On ne re-leve JAMAIS l'exception d'origine : son message peut
                # contenir des fragments du payload hub (cf. normalize.py, qui
                # interpole les valeurs de champs invalides dans son message
                # d'erreur) ou, via une future evolution de hub_client, un
                # extrait de reponse HTTP. with_dead_letter (worker_utils.py)
                # persiste str(exception) tel quel dans MongoDB -- seul le nom
                # du type est sur a propager. `from None` supprime le
                # chainage (__cause__/__context__) pour qu'aucun formateur de
                # traceback en aval ne puisse remonter au message original.
                raise RuntimeError(
                    f"sync_catalog_from_hub: echec recuperation/normalisation ({type(exc).__name__})"
                ) from None

            schema = tenant_schema_name(connection["tenant_slug"])
            tenant_session = session_factory()
            try:
                await tenant_session.execute(text(f'SET search_path TO "{schema}", public'))

                if not normalized:
                    # Un catalogue vide qui remplacerait un snapshot deja peuple n'est
                    # pas une mise a jour legitime -- c'est le signe d'un probleme cote
                    # hub (etablissement errone, reponse partielle, bug applicatif
                    # renvoyant 200 avec une liste vide). On refuse l'ecrasement plutot
                    # que de faire disparaitre silencieusement le menu public. Ce n'est
                    # pas un echec transitoire a retenter : with_dead_letter n'est pas
                    # sollicite ici, une future sync normale (cron/webhook/lazy-resync)
                    # captera une vraie mise a jour si/quand le hub en a une.
                    existing_snapshot = await snapshot_repository.get_snapshot(tenant_session, connection_id)
                    if existing_snapshot is not None and existing_snapshot.normalized:
                        logger.warning(
                            "sync_catalog_from_hub: catalogue vide refuse (snapshot existant non vide conserve) "
                            "connection_id=%s",
                            connection_id,
                        )
                        return

                await snapshot_repository.upsert_snapshot(tenant_session, connection_id, payload, normalized)
            finally:
                await tenant_session.close()

            logger.info("sync_catalog_from_hub: succes connection_id=%s produits=%s", connection_id, len(normalized))
        finally:
            if redis is not None:
                await release_sync_lock(redis, connection_id)
    finally:
        await engine.dispose()


async def sync_stale_catalog_connections(ctx) -> None:
    """Cron ARQ (horaire) : enqueue sync_catalog_from_hub pour toute connexion
    POS active dont le snapshot catalogue est absent ou perime.

    Filet de securite en complement du webhook et de la resynchronisation
    paresseuse declenchee sur lecture (HubCatalogProvider.get_catalog).

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``).
    """
    # Meme pattern que sync_catalog_from_hub ci-dessus : un seul engine cree
    # pour toute l'invocation du cron (potentiellement de nombreuses
    # connexions/sessions), dispose() une seule fois dans le finally externe.
    # Les sessions issues de session_factory partagent le pool de connexions
    # du meme engine -- pas besoin d'un engine par connexion.
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = ctx.get("redis")
    staleness = timedelta(minutes=settings.pos_hub_snapshot_staleness_minutes)

    try:
        # Garde-fou global (settings vide = feature desactivee), verifie une seule fois
        # par invocation puisqu'il s'agit d'un reglage global et non par connexion --
        # inutile de lister les connexions actives si le hub n'est meme pas configure.
        if not hub_client.is_configured():
            logger.info("sync_stale_catalog_connections: hub non configure (pos_hub_catalog_url vide), cron ignore")
            return

        session = session_factory()
        try:
            result = await session.execute(
                text(
                    "SELECT pc.id AS connection_id, t.slug AS tenant_slug "
                    "FROM public.pos_connections pc "
                    "JOIN public.tenants t ON t.id = pc.tenant_id "
                    "WHERE pc.status = 'active'"
                )
            )
            connections = result.mappings().all()
        finally:
            await session.close()

        now = datetime.now(timezone.utc)
        enqueued_count = 0
        failed_count = 0
        for row in connections:
            # Une connexion isolee (schema tenant supprime/renomme, slug obsolete,
            # erreur DB transitoire) ne doit jamais interrompre la boucle : ce cron
            # est le filet de securite horaire pour TOUTES les connexions actives,
            # une seule connexion en echec ne doit pas priver les suivantes (triees
            # apres elle) de resynchronisation pour le reste de l'heure.
            try:
                schema = tenant_schema_name(row["tenant_slug"])
                tenant_session = session_factory()
                try:
                    await tenant_session.execute(text(f'SET search_path TO "{schema}", public'))
                    snapshot = await snapshot_repository.get_snapshot(tenant_session, row["connection_id"])
                finally:
                    await tenant_session.close()

                needs_sync = snapshot is None or (now - snapshot.synced_at) > staleness
                if needs_sync and redis is not None:
                    await redis.enqueue_job("sync_catalog_from_hub", connection_id=row["connection_id"])
                    enqueued_count += 1
            except Exception as exc:
                # Type de l'exception seulement -- jamais son message (peut contenir
                # des fragments de payload/erreur DB), meme regle que
                # sync_catalog_from_hub ci-dessus.
                failed_count += 1
                logger.error(
                    "sync_stale_catalog_connections: echec verification connection_id=%s error_type=%s",
                    row["connection_id"],
                    type(exc).__name__,
                )

        logger.info(
            "sync_stale_catalog_connections: termine connexions_actives=%s enqueues=%s echecs=%s",
            len(connections),
            enqueued_count,
            failed_count,
        )
    finally:
        await engine.dispose()
