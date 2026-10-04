"""Additive stock foundations. Legacy services do not write these tables yet.

A position assigns one physical batch to exactly one establishment. NULL
remaining_quantity means unknown, never zero or usable. Reconciliation and
allocation services are deliberately deferred to sprint phase 3.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EstablishmentStock(Base):
    __tablename__ = "establishment_stock"
    __table_args__ = (
        CheckConstraint("physical_quantity >= 0", name="ck_site_stock_quantity"),
        CheckConstraint("revision > 0", name="ck_site_stock_revision"),
        CheckConstraint(
            "(reconciled_at IS NULL AND reconciled_by IS NULL) OR "
            "(reconciled_at IS NOT NULL AND reconciled_by IS NOT NULL)",
            name="ck_site_stock_reconciliation",
        ),
    )

    establishment_id: Mapped[int] = mapped_column(
        ForeignKey("establishments.id"), primary_key=True
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id"), primary_key=True
    )
    physical_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_by: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StockBatchPosition(Base):
    __tablename__ = "stock_batch_positions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["establishment_id", "ingredient_id"],
            [
                "establishment_stock.establishment_id",
                "establishment_stock.ingredient_id",
            ],
            name="fk_position_site_stock",
        ),
        ForeignKeyConstraint(
            ["batch_id", "ingredient_id"],
            ["ingredient_batches.id", "ingredient_batches.ingredient_id"],
            name="fk_position_batch_ingredient",
        ),
        UniqueConstraint(
            "batch_id", "establishment_id", "ingredient_id", name="uq_position_scope"
        ),
        CheckConstraint("remaining_quantity >= 0", name="ck_position_quantity"),
        CheckConstraint("revision > 0", name="ck_position_revision"),
        CheckConstraint(
            "(excluded_at IS NULL AND excluded_by IS NULL AND exclusion_reason IS NULL) OR "
            "(excluded_at IS NOT NULL AND excluded_by IS NOT NULL AND "
            "exclusion_reason IS NOT NULL AND length(trim(exclusion_reason)) > 0)",
            name="ck_position_exclusion_proof",
        ),
        Index("ix_position_site_ingredient", "establishment_id", "ingredient_id"),
    )

    batch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ingredient_id: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_by: Mapped[int | None] = mapped_column(Integer)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)


class StockBatchAllocation(Base):
    __tablename__ = "stock_batch_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "establishment_id"],
            ["orders.id", "orders.establishment_id"],
            name="fk_allocation_order_site",
        ),
        ForeignKeyConstraint(
            ["batch_id", "establishment_id", "ingredient_id"],
            [
                "stock_batch_positions.batch_id",
                "stock_batch_positions.establishment_id",
                "stock_batch_positions.ingredient_id",
            ],
            name="fk_allocation_position",
        ),
        UniqueConstraint("order_id", "batch_id", name="uq_allocation_order_batch"),
        CheckConstraint("quantity > 0", name="ck_allocation_quantity"),
        CheckConstraint(
            "restored_quantity >= 0 AND restored_quantity <= quantity",
            name="ck_allocation_restored",
        ),
        Index("ix_allocation_site_order", "establishment_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ingredient_id: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    restored_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, server_default="0"
    )
    allocated_by: Mapped[int | None] = mapped_column(Integer)
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EstablishmentStockMovement(Base):
    __tablename__ = "establishment_stock_movements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["establishment_id", "ingredient_id"],
            [
                "establishment_stock.establishment_id",
                "establishment_stock.ingredient_id",
            ],
            name="fk_site_movement_stock",
        ),
        ForeignKeyConstraint(
            ["batch_id", "establishment_id", "ingredient_id"],
            [
                "stock_batch_positions.batch_id",
                "stock_batch_positions.establishment_id",
                "stock_batch_positions.ingredient_id",
            ],
            name="fk_site_movement_position",
        ),
        UniqueConstraint(
            "establishment_id", "idempotency_key", name="uq_site_movement_request"
        ),
        CheckConstraint("quantity_delta <> 0", name="ck_site_movement_delta"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_site_movement_reason"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0", name="ck_site_movement_key"
        ),
        Index(
            "ix_site_movement_history",
            "establishment_id",
            "ingredient_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    establishment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ingredient_id: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[int | None] = mapped_column(Integer)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
