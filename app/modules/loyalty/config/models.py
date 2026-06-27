from datetime import date, datetime

from sqlalchemy import ARRAY, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LoyaltyConfig(Base):
    """Configuration globale du programme de fidélité par tenant (ligne unique).

    Un seul enregistrement est attendu par schéma tenant. Utiliser
    ``get_or_create_loyalty_config()`` pour un accès idempotent.
    """

    __tablename__ = "loyalty_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_ratio: Mapped[float] = mapped_column(
        Numeric(10, 4),
        default=1.0,
        server_default="1.0",
        nullable=False,
        comment="Points par euro dépensé (défaut : 1 pt/€)",
    )
    points_expiry_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="NULL = pas d'expiration. Sinon, nb de jours avant expiration des points.",
    )
    points_to_euro_rate: Mapped[float] = mapped_column(
        Numeric(10, 4),
        default=0.01,
        server_default="0.0100",
        nullable=False,
        comment="Valeur monetaire d'un point en euros (0.01 = 100 pts pour 1 EUR).",
    )
    max_cumulative_multiplier: Mapped[float] = mapped_column(
        Numeric(6, 2),
        default=20.0,
        server_default="20.00",
        nullable=False,
        comment="Plafond du multiplicateur cumule de points.",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LoyaltyRule(Base):
    """Règle de bonus de points configurable par tenant.

    Les règles sont évaluées à chaque commande. Tous les multiplicateurs applicables
    sont additionnés (ex : day_multiplier x2 + category_multiplier x1.5 → x2.5).

    Types supportés :
    - ``first_order`` : s'applique uniquement si l'utilisateur n'a aucune commande livrée.
    - ``category_multiplier`` : si un produit de la commande appartient à la catégorie.
    - ``day_multiplier`` : si le jour courant (Paris) est dans ``days_of_week``.
    - ``period_multiplier`` : si la date courante est dans [start_date, end_date].
    """

    __tablename__ = "loyalty_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="category_multiplier | period_multiplier | day_multiplier | first_order",
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        comment="Requis pour rule_type='category_multiplier'",
    )
    multiplier: Mapped[float] = mapped_column(
        Numeric(6, 4),
        nullable=False,
        comment="Ex: 2.0 pour doubler les points",
    )
    start_date: Mapped[date | None] = mapped_column(
        nullable=True,
        comment="Requis pour rule_type='period_multiplier'",
    )
    end_date: Mapped[date | None] = mapped_column(
        nullable=True,
        comment="Requis pour rule_type='period_multiplier'",
    )
    days_of_week: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer),
        nullable=True,
        comment="0=lundi, 6=dimanche. Requis pour rule_type='day_multiplier'",
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Plus haut = évalué en dernier (les multiplicateurs s'additionnent tous)",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # [🔒 SÉCURITÉ] Contraintes DB pour éviter l'inflation de points via un compte staff
    # compromis. Un multiplicateur > 10x ou une priorité > 100 est symptomatique d'une
    # injection de règle malveillante. Ces contraintes sont la dernière ligne de défense
    # même si la validation Pydantic est contournée (ex : migration directe en DB).
    __table_args__ = (
        CheckConstraint(
            "multiplier >= 1.0 AND multiplier <= 10.0",
            name="ck_loyalty_rule_multiplier_range",
        ),
        CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_loyalty_rule_priority_range",
        ),
    )


class LoyaltyReward(Base):
    """Palier de récompense échangeable contre des points de fidélité.

    Types supportés :
    - ``discount_euros`` : réduction monétaire (``discount_amount`` requis).
    - ``free_product`` : produit offert (``product_id`` requis).
    """

    __tablename__ = "loyalty_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    reward_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="discount_euros | free_product",
    )
    points_required: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_amount: Mapped[float | None] = mapped_column(
        Numeric(10, 2),
        nullable=True,
        comment="Requis si reward_type='discount_euros'",
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        comment="Requis si reward_type='free_product'",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_loyalty_rewards_points_required", "points_required"),)
