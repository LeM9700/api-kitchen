"""Modèle SQLAlchemy pour la table media_images (tenant-scoped)."""

from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MediaImage(Base):
    """Galerie d'images associées à une entité du catalogue.

    Attributes:
        id: Clé primaire auto-incrémentée.
        entity_type: Type d'entité cible (``product``, ``category``, ``extra``, ``variant``).
        entity_id: Identifiant de l'entité cible.
        cloudinary_public_id: Identifiant Cloudinary unique (utilisé pour les transformations et la suppression).
        url: URL originale de l'image.
        url_thumbnail: URL crop 300×300 (``c_fill,w_300,h_300,q_auto,f_auto``).
        url_medium: URL redimensionnée à 800px de large (``w_800,q_auto,f_auto``).
        format: Extension du fichier (``jpg``, ``png``, ``webp``, ``svg``).
        size_bytes: Poids du fichier en octets.
        width: Largeur originale en pixels.
        height: Hauteur originale en pixels.
        is_primary: Indique si c'est l'image principale de l'entité.
        display_order: Ordre d'affichage dans la galerie (croissant).
        alt_text: Texte alternatif pour l'accessibilité.
        created_at: Horodatage de création (auto, timezone-aware).
    """

    __tablename__ = "media_images"
    _raw_attrs: ClassVar[set[str]] = {
        "id",
        "entity_type",
        "entity_id",
        "cloudinary_public_id",
        "url",
        "url_thumbnail",
        "url_medium",
        "format",
        "size_bytes",
        "width",
        "height",
        "is_primary",
        "display_order",
        "alt_text",
        "created_at",
    }

    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __setattr__(self, key, value):
        if key in self._raw_attrs and "_sa_instance_state" not in self.__dict__:
            self.__dict__[key] = value
            return
        super().__setattr__(key, value)

    def __getattribute__(self, key):
        if key not in {"_raw_attrs", "__dict__", "__class__"}:
            raw_attrs = object.__getattribute__(self, "_raw_attrs")
            data = object.__getattribute__(self, "__dict__")
            if key in raw_attrs and "_sa_instance_state" not in data and key in data:
                return data[key]
        return super().__getattribute__(key)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cloudinary_public_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    url_thumbnail: Mapped[str] = mapped_column(String(512), nullable=False)
    url_medium: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    alt_text: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_media_images_entity", "entity_type", "entity_id"),
    )
