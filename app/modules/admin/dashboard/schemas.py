# app/modules/admin/stats_schemas.py
"""Schemas Pydantic stricts pour les documents MongoDB des stats admin."""
from pydantic import BaseModel


class DailyStatsResponse(BaseModel):
    date: str
    revenue: float
    order_count: int
    avg_basket: float
    tenant_slug: str


class MonthlyStatsResponse(BaseModel):
    tenant_slug: str
    year: str
    month: str
    total_orders: int
    total_revenue: float
    avg_order_value: float
    updated_at: str


class LiveStatsResponse(BaseModel):
    tenant_slug: str
    orders_last_24h: int
    revenue_last_24h: float
    avg_order_value_24h: float
    pending_orders: int
    computed_at: str


class StockAlertItem(BaseModel):
    ingredient_id: int
    name: str
    current_qty: float
    alert_threshold: float
    unit: str


class StockSnapshotResponse(BaseModel):
    tenant_slug: str
    computed_at: str
    alerts: list[StockAlertItem]


class StatsSummaryResponse(BaseModel):
    live: LiveStatsResponse
    last_day: DailyStatsResponse | None


class TopProductResponse(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    revenue: float
