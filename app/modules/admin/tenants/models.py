"""Tenant self-service configuration models."""
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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


class TenantConfig(Base):
    """Configuration self-service du tenant — ligne unique par schema tenant.

    Utilise un pattern upsert : une seule ligne par table, creee avec les
    valeurs par defaut via ``get_or_create_config()`` si absente.
    """

    __tablename__ = "tenant_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Fermeture manuelle temporaire.
    is_temporarily_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    temporary_closure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_closure_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="Nous sommes temporairement fermes. Nous vous accueillons bientot !",
        server_default="Nous sommes temporairement fermes. Nous vous accueillons bientot !",
    )

    # Calcul du temps de preparation.
    prep_time_normal_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=25, server_default="25")
    prep_time_peak_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=45, server_default="45")
    # Nombre de commandes actives declenchant le mode "heure de pointe".
    peak_orders_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    auto_calc_prep_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Overhead ajoute par commande active au-dessus du seuil de pointe.
    overhead_per_order_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    # Timezone configurable par tenant (IANA, ex: "Europe/Paris", "America/New_York").
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="Europe/Paris",
        server_default="Europe/Paris",
    )

    # [PROD] onupdate Python-side : appel callable a chaque UPDATE ORM.
    # Utilise timezone.utc explicitement (datetime.utcnow est deprecie en 3.12+).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    scheduled_close_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # [STOCK] Cooldown configurable par tenant — evite les alertes en rafale.
    # Utilise par worker/tasks/stock_alerts.py (Phase 3: API-07).
    stock_alert_cooldown_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=4,
        server_default="4",
    )
    large_stock_adjustment_threshold: Mapped[float] = mapped_column(
        Numeric(12, 3),
        nullable=False,
        default=10,
        server_default=text("10"),
    )

    # Shared printer config consumed by admin/staff apps.
    print_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    print_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Branding public (Plan 02) ─────────────────────────────────────────────
    # Ces champs sont exposés sans auth via GET /tenant/branding.
    # Ne jamais y ajouter de données sensibles.
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Format #RRGGBB strict — validé par TenantBrandingUpdate côté Pydantic.
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    # Enum applicatif : inter | poppins | playfair_display — validé Pydantic.
    font_family: Mapped[str | None] = mapped_column(String(50), nullable=True)


class BusinessHours(Base):
    """Creneau horaire d'ouverture pour un jour de la semaine.

    Plusieurs creneaux peuvent coexister pour un meme jour (``slot_index``).
    Exemple : 11h-14h (slot 0) et 18h-22h (slot 1) le lundi.
    """

    __tablename__ = "business_hours"
    __table_args__ = (
        Index("ix_business_hours_day_slot", "day_of_week", "slot_index"),
        # Contrainte DB garantissant la coherence independamment de l'ORM.
        CheckConstraint("closes_at > opens_at", name="ck_business_hours_closes_after_opens"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 0 = lundi, 6 = dimanche (align Python weekday()).
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    opens_at: Mapped[time] = mapped_column(Time(), nullable=False)
    closes_at: Mapped[time] = mapped_column(Time(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class ExceptionalClosure(Base):
    """Fermeture ponctuelle declaree pour une date precise.

    Une seule entree par date (contrainte UNIQUE sur ``closure_date``).
    """

    __tablename__ = "exceptional_closures"
    __table_args__ = (Index("ix_exceptional_closures_date", "closure_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    closure_date: Mapped[date] = mapped_column(Date(), nullable=False, unique=True)
    custom_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Si True, on affiche le default_closure_message du TenantConfig.
    use_default_message: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TenantConfigAudit(Base):
    """Audit trail des modifications de configuration tenant.

    [SECURITE] Tracabilite obligatoire de chaque changement de config.
    Pas de FK cross-schema vers ``users`` -- on stocke l'id directement.
    """

    __tablename__ = "tenant_config_audits"
    __table_args__ = (
        # Index sur changed_at pour les requetes "dernieres N modifications".
        Index("ix_tenant_config_audits_changed_at", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Reference a l'utilisateur ayant effectue la modification (public.users.id).
    changed_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Email de l'utilisateur au moment de la modification (denormalise pour lisibilite).
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Nom du champ modifie, ex: "is_temporarily_closed", "business_hours_day_1".
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Valeurs JSON-serialisees avant/apres modification.
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Contexte reseau pour la tracabilite.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
