from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, Integer, JSON, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    polygon: Mapped[dict] = mapped_column(JSON, nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    min_order_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class RestaurantDeliverySettings(Base):
    __tablename__ = "restaurant_delivery_settings"
    __table_args__ = (
        CheckConstraint(
            "restaurant_share_giveaway_points IN (0, 5, 10, 15)",
            name="ck_restaurant_delivery_settings_share_giveaway_points",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    restaurant_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    restaurant_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    independent_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    internal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    pickup_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    internal_delivery_fee: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    internal_delivery_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    internal_max_eta_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restaurant_share_giveaway_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RestaurantDeliverySettingsAudit(Base):
    __tablename__ = "restaurant_delivery_settings_audits"
    __table_args__ = (Index("ix_restaurant_delivery_settings_audits_changed_at", "changed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    changed_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
