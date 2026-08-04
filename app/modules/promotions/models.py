from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (
        Index("ix_promotions_campaign_id", "campaign_id"),
        Index("ix_promotions_user_id", "user_id"),
        Index("ix_promotions_active_dates", "is_active", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False)
    discount_value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    min_order_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # --- Limites d'usage (migration 0009) ---
    max_uses: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="NULL = illimité. Nombre total maximum d'utilisations du code.",
    )
    max_uses_per_user: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="NULL = illimité. Nombre maximum d'utilisations par utilisateur.",
    )
    current_uses: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
        comment="Compteur atomique incrémenté à chaque utilisation validée.",
    )
    first_order_only: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
        comment="Si True, le code n'est valide que pour la première commande de l'utilisateur.",
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("promotion_campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    is_stackable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    email_verified_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )


class PromotionCampaign(Base):
    __tablename__ = "promotion_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PromotionTargetCategory(Base):
    __tablename__ = "promotion_target_categories"
    __table_args__ = (
        UniqueConstraint("promotion_id", "category_id", name="uq_promo_target_category"),
        Index("ix_promo_target_categories_promo", "promotion_id"),
        Index("ix_promo_target_categories_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promotion_id: Mapped[int] = mapped_column(ForeignKey("promotions.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )


class PromotionTargetProduct(Base):
    __tablename__ = "promotion_target_products"
    __table_args__ = (
        UniqueConstraint("promotion_id", "product_id", name="uq_promo_target_product"),
        Index("ix_promo_target_products_promo", "promotion_id"),
        Index("ix_promo_target_products_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promotion_id: Mapped[int] = mapped_column(ForeignKey("promotions.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )


class PromoCodeUsage(Base):
    """Trace chaque utilisation d'un code promo par un utilisateur.

    Permet de compter rapidement ``max_uses_per_user`` via un COUNT(id)
    sur l'index ``(promo_code_id, user_id)``.
    """

    __tablename__ = "promo_code_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promotions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    # [🔒 SÉCURITÉ] Fix 5 : unicité (promo, commande) — empêche le double-enregistrement
    # d'une même commande sous le même code (résilience aux retries dans orders/service.py).
    # L'index composite (promo, user) reste pour les lookups max_uses_per_user.
    __table_args__ = (
        UniqueConstraint("promo_code_id", "order_id", name="uq_promo_usage_code_order"),
        Index("ix_promo_code_usages_promo_user", "promo_code_id", "user_id"),
    )
