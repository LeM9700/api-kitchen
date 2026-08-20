"""Modèle SQLAlchemy pour la table public.super_admins.

[🔒 SÉCURITÉ] Cette table est dans le schéma public (cross-tenant).
Elle est complètement séparée du système d'auth tenant.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PublicBase(DeclarativeBase):
    pass


class SuperAdmin(PublicBase):
    """Super administrateur plateforme — authentification indépendante des tenants.

    Attributes:
        id: Clé primaire auto-incrémentée.
        email: Adresse email unique, utilisée comme identifiant de connexion.
        password_hash: Hash bcrypt du mot de passe.
        is_active: Compte activé ou non.
        created_at: Date de création du compte.
        last_login_at: Dernière connexion réussie (mise à jour au login).
    """

    __tablename__ = "super_admins"
    __table_args__ = {"schema": "public"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
