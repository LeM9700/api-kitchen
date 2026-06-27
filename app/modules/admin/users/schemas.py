"""Schémas Pydantic pour la gestion des utilisateurs par les admins tenant."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr


class AdminUserCreate(BaseModel):
    """Corps de la requête POST /admin/users.

    Attributes:
        email: Adresse email unique du nouvel utilisateur.
        full_name: Nom complet (optionnel).
        role: Rôle restreint aux valeurs "staff" et "admin".
    """

    email: EmailStr
    full_name: str | None = None
    role: Literal["staff", "admin"]


class AdminUserOut(BaseModel):
    """Représentation d'un utilisateur retourné aux admins.

    Attributes:
        id: Identifiant primaire.
        email: Adresse email.
        full_name: Nom complet (nullable).
        role: Rôle de l'utilisateur.
        is_active: Compte actif ou désactivé.
        email_verified: True si email_verified_at est renseigné.
        created_at: Date de création du compte.
        must_change_password: Indicateur de changement de mot de passe obligatoire.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str | None
    role: str
    is_active: bool
    email_verified: bool
    created_at: datetime
    must_change_password: bool


class AdminUserCreateResponse(BaseModel):
    """Réponse au POST /admin/users incluant le mot de passe temporaire.

    Attributes:
        id: Identifiant du nouvel utilisateur.
        email: Adresse email.
        role: Rôle assigné.
        temporary_password: Mot de passe généré, à transmettre hors-bande.
    """

    id: int
    email: str
    role: str
    temporary_password: str
