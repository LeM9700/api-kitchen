"""Tâches ARQ de fidélité — expiration automatique des points.

[⚠️ PROD] La tâche ``expire_loyalty_points`` est planifiée via cron ARQ (3h00 UTC).
Elle itère sur tous les tenants actifs avec un ``asyncio.Semaphore(10)`` pour limiter
la concurrence et éviter de saturer le pool de connexions PostgreSQL.
"""

import asyncio
import logging

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.loyalty.config.service import (
    check_and_expire_points_for_all_users,
    notify_expiring_points_for_all_users,
)

logger = logging.getLogger(__name__)

_TENANT_CONCURRENCY = 10


async def expire_loyalty_points(ctx: dict) -> dict:
    """Cron task: expire loyalty points past TTL for all active tenants.

    [⚠️ PROD] Runs daily at 3am UTC via ARQ cron.
    Traite jusqu'à ``_TENANT_CONCURRENCY`` tenants simultanément.
    Les erreurs par tenant sont loguées sans interrompre les autres.

    Args:
        ctx: ARQ context (injecté automatiquement par le worker).

    Returns:
        Dictionnaire ``{"tenants_processed": int, "points_expired": int}``.
    """
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Récupère tous les slugs de tenants actifs depuis le schéma public.
    async with session_factory() as session:
        result = await session.execute(
            sa.text("SELECT slug FROM public.tenants WHERE is_active = TRUE")
        )
        slugs = [row[0] for row in result]

    semaphore = asyncio.Semaphore(_TENANT_CONCURRENCY)

    async def process_tenant(slug: str) -> tuple[int, int]:
        """Expire les points d'un tenant dans une session dédiée.

        Args:
            slug: Slug du tenant à traiter.

        Returns:
            Nombre de points expirés (0 en cas d'erreur ou d'expiration désactivée).
        """
        schema = tenant_schema_name(slug)
        async with semaphore:
            try:
                async with session_factory() as session:
                    await session.execute(text(f'SET search_path TO "{schema}", public'))
                    notified = await notify_expiring_points_for_all_users(
                        session,
                        slug,
                        redis=ctx.get("redis") or ctx.get("arq_pool"),
                    )
                    expired = await check_and_expire_points_for_all_users(session)
                    if expired:
                        logger.info(
                            "expire_loyalty_points: tenant=%s points_expired=%d expiry_notices=%d",
                            slug,
                            expired,
                            notified,
                        )
                    return expired, notified
            except Exception:
                logger.exception(
                    "expire_loyalty_points: erreur sur tenant=%s — tenant ignoré",
                    slug,
                )
                return 0, 0

    results = await asyncio.gather(*[process_tenant(slug) for slug in slugs])

    await engine.dispose()

    tenants_processed = len(slugs)
    total_expired = sum(expired for expired, _notified in results)
    total_notified = sum(notified for _expired, notified in results)

    logger.info(
        "expire_loyalty_points: terminé — %d tenants traités, %d points expirés au total",
        tenants_processed,
        total_expired,
    )
    return {
        "tenants_processed": tenants_processed,
        "points_expired": total_expired,
        "expiry_notifications_sent": total_notified,
    }
