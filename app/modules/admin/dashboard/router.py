from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_tenant_session
from app.core.http.deps import require_role
from app.modules.admin.dashboard.schemas import (
    DailyStatsResponse,
    EstablishmentGroupOverviewItem,
    GroupOverviewResponse,
    LiveStatsResponse,
    MonthlyStatsResponse,
    StatsSummaryResponse,
    StockSnapshotResponse,
    TopProductResponse,
)
from app.modules.hr.models import Establishment, Shift, TimeClockEntry
from app.modules.orders.models import Order, OrderItem
from app.modules.payments.models import Payment

router = APIRouter()

ACTIVE_ORDER_STATUSES = ("pending", "confirmed", "queued", "preparing", "ready", "out_for_delivery")
PENDING_ORDER_STATUSES = ("pending", "confirmed", "queued")


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


@router.get("/stats/group-overview", response_model=GroupOverviewResponse)
async def group_overview(
    current_user=Depends(require_role("admin")),
) -> GroupOverviewResponse:
    slug = current_user["tenant_slug"]

    async with get_tenant_session(slug) as session:
        return await build_group_overview(session)


async def build_group_overview(session: AsyncSession) -> GroupOverviewResponse:
    now = datetime.now(timezone.utc)
    late_cutoff = now - timedelta(minutes=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    establishments_result = await session.execute(
        select(Establishment).where(Establishment.is_active.is_(True)).order_by(Establishment.id)
    )
    establishments = list(establishments_result.scalars())

    active_rows = await session.execute(
        select(Order.establishment_id, func.count(Order.id))
        .where(
            Order.establishment_id.is_not(None),
            Order.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .group_by(Order.establishment_id)
    )
    pending_rows = await session.execute(
        select(Order.establishment_id, func.count(Order.id))
        .where(
            Order.establishment_id.is_not(None),
            Order.status.in_(PENDING_ORDER_STATUSES),
        )
        .group_by(Order.establishment_id)
    )
    late_rows = await session.execute(
        select(Order.establishment_id, func.count(Order.id))
        .where(
            Order.establishment_id.is_not(None),
            Order.status.in_(ACTIVE_ORDER_STATUSES),
            Order.created_at <= late_cutoff,
        )
        .group_by(Order.establishment_id)
    )
    revenue_rows = await session.execute(
        select(
            Order.establishment_id,
            func.coalesce(func.sum(func.coalesce(Payment.amount_received, Payment.amount)), 0),
        )
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.establishment_id.is_not(None),
            Payment.status.in_(("paid", "partially_refunded")),
            Payment.created_at >= today_start,
        )
        .group_by(Order.establishment_id)
    )
    present_rows = await session.execute(
        select(TimeClockEntry.establishment_id, func.count(TimeClockEntry.id))
        .where(TimeClockEntry.status.in_(("open", "break")))
        .group_by(TimeClockEntry.establishment_id)
    )
    expected_rows = await session.execute(
        select(Shift.establishment_id, func.count(Shift.id))
        .where(
            Shift.status == "scheduled",
            Shift.starts_at <= now,
            Shift.ends_at >= now,
        )
        .group_by(Shift.establishment_id)
    )

    active_by_establishment = {int(row[0]): int(row[1] or 0) for row in active_rows}
    pending_by_establishment = {int(row[0]): int(row[1] or 0) for row in pending_rows}
    late_by_establishment = {int(row[0]): int(row[1] or 0) for row in late_rows}
    revenue_by_establishment = {int(row[0]): float(row[1] or 0) for row in revenue_rows}
    present_by_establishment = {int(row[0]): int(row[1] or 0) for row in present_rows}
    expected_by_establishment = {int(row[0]): int(row[1] or 0) for row in expected_rows}

    items: list[EstablishmentGroupOverviewItem] = []
    for establishment in establishments:
        establishment_id = int(establishment.id)
        late_orders = late_by_establishment.get(establishment_id, 0)
        pending_orders = pending_by_establishment.get(establishment_id, 0)
        staff_present = present_by_establishment.get(establishment_id, 0)
        staff_expected = expected_by_establishment.get(establishment_id, 0)
        if late_orders > 0:
            status = "critical"
        elif pending_orders >= 6 or staff_present < staff_expected:
            status = "warning"
        else:
            status = "ok"

        items.append(
            EstablishmentGroupOverviewItem(
                establishment_id=establishment_id,
                establishment_name=establishment.name,
                status=status,
                active_orders=active_by_establishment.get(establishment_id, 0),
                pending_orders=pending_orders,
                late_orders=late_orders,
                revenue_today=revenue_by_establishment.get(establishment_id, 0.0),
                staff_present=staff_present,
                staff_expected=staff_expected,
            )
        )

    return GroupOverviewResponse(
        establishment_count=len(items),
        ok_count=sum(1 for item in items if item.status == "ok"),
        warning_count=sum(1 for item in items if item.status == "warning"),
        critical_count=sum(1 for item in items if item.status == "critical"),
        items=items,
    )


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
