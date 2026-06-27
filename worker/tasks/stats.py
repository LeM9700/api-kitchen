from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.orders.models import Order


async def aggregate_daily_stats(ctx, tenant_slug: str, date: str | None = None):
    target_date = datetime.fromisoformat(date) if date else datetime.now(timezone.utc) - timedelta(days=1)
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)

    async with session_factory() as session:
        await session.execute(text(f'SET search_path TO "{schema}", public'))
        revenue = await session.scalar(
            select(func.sum(Order.total)).where(
                Order.status == "delivered",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        ) or 0
        count = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.status == "delivered",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        ) or 0

    client = AsyncIOMotorClient(settings.mongo_url)
    collection = client["pizzeria_stats"][f"daily_stats_{tenant_slug}"]
    await collection.update_one(
        {"date": day_start.date().isoformat()},
        {
            "$set": {
                "date": day_start.date().isoformat(),
                "revenue": float(revenue),
                "order_count": count,
                "avg_basket": float(revenue) / count if count else 0,
                "tenant_slug": tenant_slug,
            }
        },
        upsert=True,
    )
    client.close()
    await engine.dispose()


async def _get_all_tenant_slugs(engine) -> list[str]:
    """Retourne la liste de tous les slugs tenant depuis public.tenants.

    Args:
        engine: Engine SQLAlchemy async à utiliser pour la requête.

    Returns:
        Liste de slugs (ex: ``["pizza-roma", "burger-paris"]``).
    """
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        result = await session.execute(text("SELECT slug FROM public.tenants"))
        return [row[0] for row in result]


async def aggregate_monthly_stats(ctx) -> None:
    """Agrège les statistiques journalières par mois pour tous les tenants.

    Lit la collection ``daily_stats_{tenant_slug}`` dans MongoDB, groupe par
    année + mois, et écrit (upsert) dans ``monthly_stats_{tenant_slug}`` avec
    la moyenne d'ordre pondérée par le nombre de commandes.

    Planifié via arq cron : 1x/jour à minuit UTC.

    Args:
        ctx: Contexte ARQ (injecté automatiquement par le worker).
    """
    engine = create_async_engine(settings.database_url)
    client = AsyncIOMotorClient(settings.mongo_url)
    db = client[settings.mongo_db]

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)
        for slug in tenant_slugs:
            pipeline = [
                {
                    "$group": {
                        "_id": {
                            "year": {"$substr": ["$date", 0, 4]},
                            "month": {"$substr": ["$date", 5, 2]},
                        },
                        "total_orders": {"$sum": "$order_count"},
                        "total_revenue": {"$sum": "$revenue"},
                        # Somme pondérée pour calculer avg_order_value exact.
                        "weighted_avg_sum": {
                            "$sum": {"$multiply": ["$avg_basket", "$order_count"]}
                        },
                    }
                }
            ]
            cursor = db[f"daily_stats_{slug}"].aggregate(pipeline)
            async for doc in cursor:
                year = doc["_id"]["year"]
                month = doc["_id"]["month"]
                total_orders = doc["total_orders"]
                total_revenue = doc["total_revenue"]
                avg_order_value = (
                    doc["weighted_avg_sum"] / total_orders if total_orders else 0.0
                )
                await db[f"monthly_stats_{slug}"].update_one(
                    {"tenant_slug": slug, "year": year, "month": month},
                    {
                        "$set": {
                            "tenant_slug": slug,
                            "year": year,
                            "month": month,
                            "total_orders": total_orders,
                            "total_revenue": float(total_revenue),
                            "avg_order_value": float(avg_order_value),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    upsert=True,
                )
    finally:
        client.close()
        await engine.dispose()


async def aggregate_live_stats(ctx) -> None:
    """Calcule les statistiques temps réel des dernières 24 h pour tous les tenants.

    Lit les commandes ``delivered`` des dernières 24 h depuis PostgreSQL (source
    de vérité) et écrit (upsert) dans la collection MongoDB ``live_dashboard_{slug}``,
    lue par ``/admin/stats/live``.

    Planifié via arq cron : toutes les 5 minutes.

    Args:
        ctx: Contexte ARQ (injecté automatiquement par le worker).
    """
    engine = create_async_engine(settings.database_url)
    client = AsyncIOMotorClient(settings.mongo_url)
    db = client[settings.mongo_db]
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        for slug in tenant_slugs:
            schema = tenant_schema_name(slug)
            async with session_factory() as session:
                await session.execute(text(f'SET search_path TO "{schema}", public'))
                revenue_24h = await session.scalar(
                    select(func.sum(Order.total)).where(
                        Order.status == "delivered",
                        Order.created_at >= window_start,
                    )
                ) or 0
                orders_24h = await session.scalar(
                    select(func.count()).select_from(Order).where(
                        Order.status == "delivered",
                        Order.created_at >= window_start,
                    )
                ) or 0
                pending_count = await session.scalar(
                    select(func.count()).select_from(Order).where(
                        Order.status == "pending",
                    )
                ) or 0

            await db[f"live_dashboard_{slug}"].update_one(
                {"tenant_slug": slug},
                {
                    "$set": {
                        "tenant_slug": slug,
                        "orders_last_24h": orders_24h,
                        "revenue_last_24h": float(revenue_24h),
                        "avg_order_value_24h": (
                            float(revenue_24h) / orders_24h if orders_24h else 0.0
                        ),
                        "pending_orders": pending_count,
                        "computed_at": now.isoformat(),
                    }
                },
                upsert=True,
            )
    finally:
        client.close()
        await engine.dispose()
