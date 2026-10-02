from datetime import datetime

from datetime import date, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"
    __table_args__ = (
        Index("ix_delivery_zones_establishment_id", "establishment_id"),
        CheckConstraint(
            "shape_kind IN ('polygon', 'circle', 'isochrone')", name="ck_delivery_zones_shape_kind"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Etablissement qui livre cette zone. NULL = zone historique (creee avant le
    # rattachement par etablissement) : elle reste valable pour tous les etablissements
    # jusqu'a ce qu'un admin la rattache.
    establishment_id: Mapped[int | None] = mapped_column(
        ForeignKey("establishments.id"), nullable=True
    )
    # Geometrie utilisee pour la verification d'adresse : toujours un Polygon valide,
    # quel que soit le mode de saisie (dessin, cercle, temps de trajet).
    polygon: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Mode de saisie et ses parametres (centre, rayon, minutes...) pour rouvrir la zone
    # dans l'editeur telle que l'admin l'a creee.
    shape_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="polygon", server_default="polygon"
    )
    shape_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fee: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    min_order_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class DeliveryZoneRule(Base):
    """Regle de tarification d'une zone : tarif different (``fee``) ou livraison offerte
    (``free``), eventuellement limitee a des jours, des horaires, une periode ou a partir
    d'un montant de panier. Evaluee cote serveur par ``delivery.pricing``."""

    __tablename__ = "delivery_zone_rules"
    __table_args__ = (
        Index("ix_delivery_zone_rules_zone_id", "zone_id"),
        CheckConstraint("kind IN ('fee', 'free')", name="ck_delivery_zone_rules_kind"),
        CheckConstraint(
            "(kind = 'fee' AND fee IS NOT NULL) OR kind = 'free'", name="ck_delivery_zone_rules_fee"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_zones.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    fee: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # La regle ne s'applique qu'a partir de ce sous-total (ex. livraison offerte des 25 EUR).
    min_subtotal: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Jours concernes, 0 = lundi ... 6 = dimanche. NULL = tous les jours.
    days_of_week: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Fenetre horaire locale a l'etablissement. Si end_time <= start_time, la fenetre
    # passe minuit. NULL = toute la journee.
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # En cas de regles tarifaires concurrentes, la plus haute priorite gagne.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


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
