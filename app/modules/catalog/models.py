from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "preparation_station IN ('kitchen', 'counter', 'none')",
            name="ck_categories_preparation_station",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    preparation_station: Mapped[str] = mapped_column(
        String(16), nullable=False, default="kitchen", server_default="kitchen"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "preparation_station IS NULL OR preparation_station IN ('kitchen', 'counter', 'none')",
            name="ck_products_preparation_station",
        ),
        Index(
            "idx_products_fts",
            text("to_tsvector('french', name || ' ' || COALESCE(description, ''))"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preparation_station: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_delivery_prohibited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ProductAvailabilityOverride(Base):
    __tablename__ = "product_availability_overrides"
    __table_args__ = (
        Index("ix_product_availability_overrides_product_created", "product_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductVariant(Base):
    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price_delta: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Extra(Base):
    __tablename__ = "extras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class ProductExtra(Base):
    __tablename__ = "product_extras"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    extra_id: Mapped[int] = mapped_column(ForeignKey("extras.id", ondelete="CASCADE"), primary_key=True)


class ExtraIngredient(Base):
    __tablename__ = "extra_ingredients"
    __table_args__ = (
        Index("ix_extra_ingredients_extra", "extra_id"),
        Index("ix_extra_ingredients_ingredient", "ingredient_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    extra_id: Mapped[int] = mapped_column(ForeignKey("extras.id", ondelete="CASCADE"), nullable=False)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)


class ProductRecommendation(Base):
    __tablename__ = "product_recommendations"
    __table_args__ = (
        Index("ix_product_recommendations_product", "product_id"),
        Index("ix_product_recommendations_recommended", "recommended_product_id"),
        UniqueConstraint(
            "product_id", "recommended_product_id", name="uq_product_recommendations_pair"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    recommended_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")


class CatalogPriceAudit(Base):
    __tablename__ = "catalog_price_audits"
    __table_args__ = (
        Index("ix_catalog_price_audits_entity", "entity_type", "entity_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    old_price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    new_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class CatalogImportBatch(Base):
    __tablename__ = "catalog_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    csv_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="dry_run", server_default="dry_run")
    validation_report: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
