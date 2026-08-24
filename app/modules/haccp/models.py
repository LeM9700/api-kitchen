"""Modèles ORM SQLAlchemy — module HACCP / sécurité alimentaire.

Toutes les tables vivent dans le schéma tenant (``tenant_{slug}``).
Elles sont enregistrées dans ``app/core/database/tenant_models.py``.

Règles métier clés :
- ``haccp_check_sessions`` est le gate bloquant ouverture/fermeture.
  Une session ``status='complete'`` est requise avant que l'app client
  soit accessible (ouverture) ou que la fermeture soit confirmée.
- ``dlc_level`` : 1 = emballage brut, 2 = conservation frigo/congélateur,
  3 = produit en cours d'utilisation (table à garniture).
- ``haccp_frying_oil_logs`` est optionnel — activé via feature flag tenant.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HaccpEquipment(Base):
    """Équipement à contrôler : frigo, congélateur, chambre froide, etc."""

    __tablename__ = "haccp_equipment"
    __table_args__ = (
        CheckConstraint(
            "type IN ('fridge','freezer','cold_room','hot_hold','ambient')",
            name="ck_haccp_equipment_type",
        ),
        Index("ix_haccp_equipment_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_min_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_max_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    check_at_opening: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    check_at_closing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpCheckSession(Base):
    """Session de contrôle HACCP (ouverture ou fermeture).

    [⚠️ PROD] Gate bloquant : ``status='complete'`` requis avant toute
    confirmation d'ouverture ou de fermeture du restaurant.
    Contrainte UNIQUE (date, session_type) — une seule session par type par jour.
    """

    __tablename__ = "haccp_check_sessions"
    __table_args__ = (
        CheckConstraint("session_type IN ('opening','closing')", name="ck_haccp_sessions_type"),
        CheckConstraint(
            "status IN ('in_progress','complete','incomplete_validated')",
            name="ck_haccp_sessions_status",
        ),
        UniqueConstraint("date", "session_type", name="uq_haccp_sessions_date_type"),
        Index("ix_haccp_check_sessions_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    started_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="in_progress")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpTemperatureLog(Base):
    """Relevé de température pour un équipement dans une session."""

    __tablename__ = "haccp_temperature_logs"
    __table_args__ = (Index("ix_haccp_temp_logs_session", "session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_check_sessions.id", ondelete="CASCADE"), nullable=False
    )
    equipment_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_equipment.id"), nullable=False
    )
    measured_temp: Mapped[float] = mapped_column(Float, nullable=False)
    is_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    corrective_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpDlcCheck(Base):
    """Vérification DLC (3 niveaux) pour un ingrédient dans une session.

    dlc_level :
        1 = date sur emballage produit brut
        2 = date limite conservation frigo/congélateur
        3 = date limite produit en utilisation (table à garniture)
    """

    __tablename__ = "haccp_dlc_checks"
    __table_args__ = (
        CheckConstraint("dlc_level IN (1,2,3)", name="ck_haccp_dlc_level"),
        Index("ix_haccp_dlc_checks_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_check_sessions.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingredient_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dlc_level: Mapped[int] = mapped_column(Integer, nullable=False)
    dlc_date: Mapped[date] = mapped_column(Date, nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    corrective_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpCleaningTask(Base):
    """Tâche de nettoyage-désinfection définie par l'admin (plan ND)."""

    __tablename__ = "haccp_cleaning_tasks"
    __table_args__ = (
        CheckConstraint(
            "frequency IN ('daily','weekly','monthly','per_service')",
            name="ck_haccp_cleaning_frequency",
        ),
        CheckConstraint(
            "session_type IN ('opening','closing','both')",
            name="ck_haccp_cleaning_session_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    zone: Mapped[str] = mapped_column(String(64), nullable=False)
    frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="both")
    product_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required_role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpCleaningLog(Base):
    """Réalisation d'une tâche ND par le staff lors d'une session."""

    __tablename__ = "haccp_cleaning_logs"
    __table_args__ = (
        UniqueConstraint("session_id", "task_id", name="uq_haccp_cleaning_log"),
        Index("ix_haccp_cleaning_logs_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_check_sessions.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_cleaning_tasks.id"), nullable=False
    )
    completed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class HaccpNonConformity(Base):
    """Non-conformité + action corrective + validation manager."""

    __tablename__ = "haccp_non_conformities"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('temperature','dlc','cleaning','reception','cooling','other')",
            name="ck_haccp_nc_source",
        ),
        CheckConstraint("status IN ('open','in_progress','closed')", name="ck_haccp_nc_status"),
        Index("ix_haccp_nc_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("haccp_check_sessions.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    corrective_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpReceptionControl(Base):
    """Contrôle à réception d'une livraison fournisseur."""

    __tablename__ = "haccp_reception_controls"
    __table_args__ = (Index("ix_haccp_reception_date", "delivery_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_name: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    temperature_on_arrival: Mapped[float | None] = mapped_column(Float, nullable=True)
    packaging_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    labeling_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dlc_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpCoolingLog(Base):
    """Suivi de refroidissement rapide (réglementation : ≤10°C en 2h)."""

    __tablename__ = "haccp_cooling_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_name: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temp_start: Mapped[float] = mapped_column(Float, nullable=False)
    temp_at_90min: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_final: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_compliant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    corrective_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpTrainingRecord(Base):
    """Enregistrement de formation hygiène (arrêté 12/02/2024, 14h obligatoires)."""

    __tablename__ = "haccp_training_records"
    __table_args__ = (
        CheckConstraint(
            "training_type IN ('hygiene_14h','refresher','haccp_module','other')",
            name="ck_haccp_training_type",
        ),
        Index("ix_haccp_training_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    training_type: Mapped[str] = mapped_column(String(64), nullable=False)
    training_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certificate_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HaccpFryingOilLog(Base):
    """Suivi qualité huile de friture (optionnel — feature flag par tenant).

    Seuil légal : polarity_percent < 25% (AGL).
    """

    __tablename__ = "haccp_frying_oil_logs"
    __table_args__ = (
        CheckConstraint(
            "action_taken IN ('none','filtered','replaced') OR action_taken IS NULL",
            name="ck_haccp_oil_action",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("haccp_check_sessions.id", ondelete="CASCADE"), nullable=False
    )
    fryer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    color_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    odor_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_compliant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    action_taken: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
