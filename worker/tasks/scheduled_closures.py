"""Cron ARQ : applique les fermetures programmees des tenants."""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name

logger = logging.getLogger(__name__)


async def _get_active_tenant_slugs(engine) -> list[str]:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT slug FROM public.tenants "
                "WHERE COALESCE(is_suspended, false) = false"
            )
        )
        return [row[0] for row in result]


async def process_scheduled_closures(ctx) -> None:
    """Ferme automatiquement les tenants dont scheduled_close_at est arrive."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    try:
        tenant_slugs = await _get_active_tenant_slugs(engine)
        for slug in tenant_slugs:
            schema = tenant_schema_name(slug)
            try:
                async with session_factory() as session:
                    await session.execute(text(f'SET search_path TO "{schema}", public'))
                    result = await session.execute(
                        text(
                            "SELECT id, scheduled_close_at, is_temporarily_closed "
                            "FROM tenant_config "
                            "WHERE scheduled_close_at IS NOT NULL "
                            "AND scheduled_close_at <= :now "
                            "LIMIT 1"
                        ),
                        {"now": now},
                    )
                    row = result.fetchone()
                    if row is None:
                        continue

                    await session.execute(
                        text(
                            "UPDATE tenant_config "
                            "SET is_temporarily_closed = true, "
                            "scheduled_close_at = NULL, updated_at = now() "
                            "WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO tenant_config_audits "
                            "(changed_by_user_id, field_name, old_value, new_value, user_email) "
                            "VALUES "
                            "(0, 'is_temporarily_closed', :old_closed, 'true', 'system'), "
                            "(0, 'scheduled_close_at', :old_scheduled, 'null', 'system')"
                        ),
                        {
                            "old_closed": json.dumps(row.is_temporarily_closed),
                            "old_scheduled": json.dumps(row.scheduled_close_at, default=str),
                        },
                    )
                    await session.commit()

                arq_pool = ctx.get("redis") if ctx else None
                if arq_pool is not None:
                    await arq_pool.enqueue_job(
                        "notify_config_change",
                        tenant_slug=slug,
                        is_closed=True,
                    )

            except Exception as exc:
                logger.error("process_scheduled_closures: tenant=%s failed: %s", slug, exc)
                continue
    finally:
        await engine.dispose()
