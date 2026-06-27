from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.http.deps import require_role
from app.modules.admin.dashboard.schemas import (
    DailyStatsResponse,
    LiveStatsResponse,
    MonthlyStatsResponse,
    StatsSummaryResponse,
    StockSnapshotResponse,
)

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
