from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KdsScreen(Base):
    __tablename__ = "kds_screens"
    __table_args__ = (
        UniqueConstraint("screen_key", name="uq_kds_screens_screen_key"),
        CheckConstraint("mode IN ('kitchen', 'counter', 'service')", name="ck_kds_screens_mode"),
        CheckConstraint("interaction_mode IN ('wall', 'touch')", name="ck_kds_screens_interaction_mode"),
        CheckConstraint("tickets_per_page BETWEEN 1 AND 8", name="ck_kds_screens_tickets_per_page"),
        CheckConstraint("length(btrim(station)) BETWEEN 1 AND 64", name="ck_kds_screens_station_not_blank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    screen_key: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="kitchen", server_default="kitchen")
    station: Mapped[str] = mapped_column(String(64), nullable=False, default="kitchen", server_default="kitchen")
    interaction_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="wall", server_default="wall")
    tickets_per_page: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KdsPairingCode(Base):
    __tablename__ = "kds_pairing_codes"
    __table_args__ = (
        Index("ix_kds_pairing_codes_code_hash", "code_hash"),
        Index("ix_kds_pairing_codes_expires_at", "expires_at"),
        Index("ix_kds_pairing_codes_screen_id", "screen_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("kds_screens.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KdsRemoteSession(Base):
    __tablename__ = "kds_remote_sessions"
    __table_args__ = (
        UniqueConstraint("session_token_hash", name="uq_kds_remote_sessions_session_token_hash"),
        Index("ix_kds_remote_sessions_screen_id", "screen_id"),
        Index("ix_kds_remote_sessions_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    screen_id: Mapped[int] = mapped_column(ForeignKey("kds_screens.id", ondelete="CASCADE"), nullable=False)
    paired_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
