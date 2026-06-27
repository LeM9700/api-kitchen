"""Cron ARQ : snapshot horaire des ingrédients sous seuil d'alerte.

Alimente la collection MongoDB ``stock_snapshots_{slug}`` lue par
``GET /admin/stats/stock``. Enqueue ``send_stock_alert`` pour chaque
ingrédient éligible (cooldown 4h vérifié dans la task aval).
"""
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.stock.models import Ingredient

from worker.tasks.stats import _get_all_tenant_slugs

logger = logging.getLogger(__name__)


async def aggregate_stock_snapshot(ctx) -> None:
    """Calcule le snapshot stock pour tous les tenants et alerte si nécessaire.

    Pour chaque tenant :
    1. Requête les ingrédients dont ``current_qty <= alert_threshold``.
    2. Upsert le document dans ``stock_snapshots_{slug}`` MongoDB.
    3. Enqueue ``send_stock_alert`` pour chaque ingrédient sous seuil.
       Le cooldown 4h est évalué dans ``send_stock_alert`` (non ici).

    Planifié toutes les heures à ``minute=0``.

    Args:
        ctx: Contexte ARQ injecté automatiquement (contient ``redis``).
    """
    engine = create_async_engine(settings.database_url)
    client = AsyncIOMotorClient(settings.mongo_url)
    db = client[settings.mongo_db]
    now = datetime.now(timezone.utc)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        for slug in tenant_slugs:
            schema = tenant_schema_name(slug)
            try:
                async with session_factory() as session:
                    await session.execute(text(f'SET search_path TO "{schema}", public'))
                    result = await session.execute(
                        select(Ingredient).where(
                            Ingredient.current_qty <= Ingredient.alert_threshold
                        )
                    )
                    under_threshold = list(result.scalars().all())

                alerts = [
                    {
                        "ingredient_id": ing.id,
                        "name": ing.name,
                        "current_qty": float(ing.current_qty),
                        "alert_threshold": float(ing.alert_threshold),
                        "unit": ing.unit,
                    }
                    for ing in under_threshold
                ]

                await db[f"stock_snapshots_{slug}"].update_one(
                    {"tenant_slug": slug},
                    {
                        "$set": {
                            "tenant_slug": slug,
                            "computed_at": now.isoformat(),
                            "alerts": alerts,
                        }
                    },
                    upsert=True,
                )

                # Enqueue alerte pour chaque ingrédient sous seuil.
                arq_pool = ctx.get("redis")
                if arq_pool is not None:
                    for ing in under_threshold:
                        try:
                            await arq_pool.enqueue_job(
                                "send_stock_alert",
                                ingredient_id=ing.id,
                                ingredient_name=ing.name,
                                current_qty=float(ing.current_qty),
                                tenant_slug=slug,
                            )
                        except Exception as exc:
                            logger.error(
                                "aggregate_stock_snapshot: enqueue failed tenant=%s ingredient=%s: %s",
                                slug,
                                ing.name,
                                exc,
                            )

            except Exception as exc:
                logger.error(
                    "aggregate_stock_snapshot: erreur tenant=%s: %s", slug, exc
                )
                continue

    finally:
        client.close()
        await engine.dispose()
