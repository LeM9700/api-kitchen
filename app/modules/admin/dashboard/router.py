from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import get_tenant_session
from app.core.http.deps import require_role
from app.modules.admin.dashboard.schemas import (
    DailyStatsResponse,
    LiveStatsResponse,
    MonthlyStatsResponse,
    StatsSummaryResponse,
    StockSnapshotResponse,
    TopProductResponse,
)
from app.modules.orders.models import Order, OrderItem

router = APIRouter()


def get_mongo(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.motor_client[settings.mongo_db]


@router.get("/stats/daily", response_model=list[DailyStatsResponse])
async def daily_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> list[DailyStatsResponse]:
    slug = current_user["tenant_slug"]
    docs = await db[f"daily_stats_{slug}"].find().sort("date", -1).limit(30).to_list(30)
    return [DailyStatsResponse(**{k: v for k, v in doc.items() if k != "_id"}) for doc in docs]


@router.get("/stats/monthly", response_model=list[MonthlyStatsResponse])
async def monthly_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> list[MonthlyStatsResponse]:
    slug = current_user["tenant_slug"]
    docs = await db[f"monthly_stats_{slug}"].find().sort("month", -1).limit(12).to_list(12)
    return [MonthlyStatsResponse(**{k: v for k, v in doc.items() if k != "_id"}) for doc in docs]


@router.get("/stats/live", response_model=LiveStatsResponse | dict)
async def live_stats(
    current_user=Depends(require_role("staff", "admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> LiveStatsResponse | dict:
    slug = current_user["tenant_slug"]
    doc = await db[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
    if not doc:
        return {}
    return LiveStatsResponse(**{k: v for k, v in doc.items() if k != "_id"})


@router.get("/stats/stock", response_model=StockSnapshotResponse | dict)
async def stock_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> StockSnapshotResponse | dict:
    slug = current_user["tenant_slug"]
    doc = await db[f"stock_snapshots_{slug}"].find_one({"tenant_slug": slug})
    if not doc:
        return {}
    return StockSnapshotResponse(**{k: v for k, v in doc.items() if k != "_id"})


@router.get("/stats/summary", response_model=StatsSummaryResponse)
async def stats_summary(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> StatsSummaryResponse:
    slug = current_user["tenant_slug"]

    live_doc = await db[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
    live = (
        LiveStatsResponse(**{k: v for k, v in live_doc.items() if k != "_id"})
        if live_doc
        else LiveStatsResponse(
            tenant_slug=slug,
            orders_last_24h=0,
            revenue_last_24h=0.0,
            avg_order_value_24h=0.0,
            pending_orders=0,
            computed_at="",
        )
    )

    daily_docs = await db[f"daily_stats_{slug}"].find().sort("date", -1).limit(1).to_list(1)
    last_day = (
        DailyStatsResponse(**{k: v for k, v in daily_docs[0].items() if k != "_id"})
        if daily_docs
        else None
    )

    return StatsSummaryResponse(live=live, last_day=last_day)


@router.get("/stats/top-products", response_model=list[TopProductResponse])
async def top_products(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    current_user=Depends(require_role("admin")),
) -> list[TopProductResponse]:
    slug = current_user["tenant_slug"]
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_tenant_session(slug) as session:
        result = await session.execute(
            select(
                OrderItem.product_id,
                func.coalesce(func.max(OrderItem.product_name_snapshot), "").label("product_name"),
                func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity"),
                func.coalesce(func.sum(OrderItem.total + OrderItem.extras_total), 0).label("revenue"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.created_at >= since, Order.status != "cancelled")
            .group_by(OrderItem.product_id)
            .order_by(func.coalesce(func.sum(OrderItem.quantity), 0).desc())
            .limit(limit)
        )

    return [
        TopProductResponse(
            product_id=int(row.product_id),
            product_name=row.product_name or f"Produit #{row.product_id}",
            quantity=int(row.quantity or 0),
            revenue=float(row.revenue or 0),
        )
        for row in result
    ]
