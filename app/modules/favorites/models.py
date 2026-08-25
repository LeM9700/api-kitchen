from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Favorite(Base):
    """Produit marqué favori par un client.

    Un utilisateur peut avoir N favoris. Un même produit ne peut être
    favori qu'une fois par utilisateur — garanti par la contrainte
    ``uq_favorite_user_product``.

    Attributes:
        id: Clé primaire auto-incrémentée.
        user_id: Référence à l'utilisateur propriétaire (sans FK pour éviter
            une dépendance croisée entre modules — même convention que
            ``notifications.models.DeviceToken``).
        product_id: Référence au produit favori (sans FK, même raison).
        created_at: Timestamp de création (UTC, auto).
    """

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_favorite_user_product"),
        Index("ix_favorites_user_id", "user_id"),
    )
