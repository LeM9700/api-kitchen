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
    # Dispatch par livreurs : quand il est actif, `ready -> out_for_delivery` exige un livreur
    # assigne (impose par orders.update_status). Coupe par defaut : un restaurant qui n'a pas
    # encore de livreurs enregistres continue de partir en livraison comme avant.
    driver_dispatch_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Preuve de remise : quand elle est exigee, le livreur doit saisir le code a 4 chiffres du
    # client (sinon seule une livraison « sans code » d'un administrateur, motivee et tracee).
    # Coupee par defaut : les anciennes apps client n'affichent pas le code.
    delivery_proof_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Regles d'echec « client absent / injoignable » : delai d'attente depuis l'arrivee et nombre
    # minimal d'appels avant de pouvoir declarer l'echec.
    failure_min_wait_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    failure_min_call_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
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


# --------------------------------------------------------------------------- livreurs (phase 3)


class DriverProfile(Base):
    """Livreur salarie du restaurant. Le compte est un ``users`` de role ``driver`` (aucun acces
    hors pointage et livraisons, impose cote API par ``require_role``). Le pointage passe par
    l'``EmployeeProfile`` du meme utilisateur (module RH)."""

    __tablename__ = "driver_profiles"
    __table_args__ = (Index("ix_driver_profiles_establishment_id", "establishment_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    # Un livreur travaille pour un seul etablissement (celui de son profil employe).
    establishment_id: Mapped[int] = mapped_column(ForeignKey("establishments.id"), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vehicle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Inactif : ne recoit plus de livraison et n'a plus acces a l'ecran de livraison.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Consentement du livreur au partage de sa position pendant une livraison (voir
    # delivery/tracking.py). Sans lui, aucune position n'est acceptee. Retire = NULL.
    location_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location_consent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeliveryRun(Base):
    """Tournee : les livraisons que le livreur emmene en un seul depart."""

    __tablename__ = "delivery_runs"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'completed')", name="ck_delivery_runs_status"),
        Index("ix_delivery_runs_driver_id_status", "driver_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("driver_profiles.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Statuts « vivants » d'une livraison : une commande n'a qu'une livraison vivante a la fois.
DELIVERY_ACTIVE_STATUSES = ("assigned", "out_for_delivery", "arrived")


class Delivery(Base):
    """Livraison d'une commande a un livreur. Suit le statut de la commande (voir
    ``delivery/lifecycle.py``) ; ``arrived`` est propre a la livraison."""

    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('assigned', 'out_for_delivery', 'arrived', 'delivered', 'failed', 'cancelled')",
            name="ck_deliveries_status",
        ),
        Index("ix_deliveries_driver_id_status", "driver_id", "status"),
        Index("ix_deliveries_order_id", "order_id"),
        Index(
            "uq_deliveries_one_active_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status IN ('assigned', 'out_for_delivery', 'arrived')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    driver_id: Mapped[int] = mapped_column(ForeignKey("driver_profiles.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_runs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="assigned", server_default="assigned")
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assigned_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeliveryEvent(Base):
    """Journal d'une livraison (qui a attribue, reattribue, retire, parti, arrive, livre)."""

    __tablename__ = "delivery_events"
    __table_args__ = (Index("ix_delivery_events_order_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("deliveries.id"), nullable=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    driver_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryCodeAttempt(Base):
    """Essai de saisie du code de remise (reussi ou non), journalise."""

    __tablename__ = "delivery_code_attempts"
    __table_args__ = (Index("ix_delivery_code_attempts_delivery_id", "delivery_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


FAILURE_REASONS = {
    # motif -> a qui incombe l'echec
    "customer_absent": "customer",
    "customer_unreachable": "customer",
    "wrong_address": "customer",
    "customer_refused": "customer",
    "order_problem": "restaurant",
    "other": "customer",
}


class DeliveryFailure(Base):
    """Echec de livraison declare par le livreur, a traiter par un administrateur
    (rembourser, retenir des frais, ou relivrer)."""

    __tablename__ = "delivery_failures"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'resolved')", name="ck_delivery_failures_status"),
        CheckConstraint("fault IN ('customer', 'restaurant')", name="ck_delivery_failures_fault"),
        Index("ix_delivery_failures_status", "status"),
        Index("ix_delivery_failures_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    driver_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    fault: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    call_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    waited_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # refund | retain | redeliver
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    retained_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- GPS livreur (phase 5)


class DriverLocationPoint(Base):
    """Historique **echantillonne** des positions d'un livreur pendant une livraison active.
    Purge au-dela de ``gps_retention_hours`` (au moins 96 h) par une tache planifiee."""

    __tablename__ = "driver_location_points"
    __table_args__ = (
        Index("ix_driver_location_points_driver_id_recorded_at", "driver_id", "recorded_at"),
        Index("ix_driver_location_points_recorded_at", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("driver_profiles.id"), nullable=False)
    # Tournee en cours au moment de la mesure (pour retrouver une course en cas de litige).
    run_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_runs.id"), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DriverLastLocation(Base):
    """Derniere position connue de chaque livreur (une ligne par livreur, ecrasee a chaque envoi)."""

    __tablename__ = "driver_last_locations"

    driver_id: Mapped[int] = mapped_column(ForeignKey("driver_profiles.id"), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_runs.id"), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
