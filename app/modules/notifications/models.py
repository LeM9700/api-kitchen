from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeviceToken(Base):
    """Token push d'un appareil mobile enregistré par un utilisateur.

    Un utilisateur peut enregistrer N appareils (multi-device). Chaque token
    est unique par couple (user_id, token) — garantie par la contrainte
    ``uq_device_token_user``.

    Attributes:
        id: Clé primaire auto-incrémentée.
        user_id: Référence à l'utilisateur propriétaire (sans FK pour éviter
            une dépendance croisée entre modules).
        platform: Plateforme cible — ``"ios"`` ou ``"android"``.
        token: Token APNs ou FCM, jusqu'à 512 caractères.
        device_name: Nom lisible de l'appareil (optionnel, pour debug/UI).
        is_active: False si le token a été invalidé par APNs (410) ou FCM
            (UNREGISTERED). Utilisé pour filtrer les envois futurs.
        created_at: Timestamp de création (UTC, auto).
        last_used_at: Timestamp du dernier push réussi (UTC, nullable).
    """

    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(10))  # "ios" | "android"
    token: Mapped[str] = mapped_column(String(512))
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "token", name="uq_device_token_user"),
        Index("ix_device_tokens_user_active", "user_id", "is_active"),
    )
