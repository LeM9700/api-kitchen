from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_orders_user_id_idempotency_key"),
        CheckConstraint("order_type IN ('delivery', 'pickup', 'dine_in')", name="ck_orders_order_type"),
        CheckConstraint("source IN ('customer', 'manual', 'system')", name="ck_orders_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 'delivery' | 'pickup' | 'dine_in' -- contrainte CHECK en base (cf. migrations et
    # _TENANT_DDL_STATEMENTS). String plutot qu'un type ENUM Postgres natif, pour
    # rester coherent avec status/payment_status/role deja en place dans ce module.
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="delivery", server_default="delivery")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    payment_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="customer", server_default="customer")
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    discount_total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    delivery_fee: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_zone_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Stocké pour permettre la désactivation du code promo à la confirmation de paiement.
    # Nécessite une migration Alembic : ALTER TABLE orders ADD COLUMN promo_code VARCHAR(64).
    promo_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint(
            "preparation_status IN ('pending', 'preparing', 'ready')",
            name="ck_order_items_preparation_status",
        ),
        CheckConstraint(
            "preparation_station IN ('kitchen', 'counter', 'none')",
            name="ck_order_items_preparation_station",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    variant_name_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extras_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    extras_total: Mapped[float] = mapped_column(
        Numeric(10, 2), default=0, nullable=False, server_default=text("0")
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    preparation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    preparation_station: Mapped[str] = mapped_column(
        String(16), nullable=False, default="kitchen", server_default="kitchen"
    )
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prepared_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    __table_args__ = (
        CheckConstraint("authority IN ('internal', 'external')", name="ck_order_status_history_authority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'internal' | 'external' -- qui a impose la transition (cf. TransitionAuthority
    # dans orders/service.py). CHECK constraint en base, cf. migrations et
    # _TENANT_DDL_STATEMENTS. VARCHAR plutot qu'un type ENUM Postgres natif, pour
    # rester coherent avec status/payment_status deja en place dans ce module.
    authority: Mapped[str] = mapped_column(String(16), nullable=False, default="internal", server_default="internal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
