"""Schémas Pydantic pour le module super-admin."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class SuperAdminLoginRequest(BaseModel):
    """Corps de la requête POST /super-admin/login."""

    email: EmailStr
    password: str


class SuperAdminTokenResponse(BaseModel):
    """Réponse au login super-admin."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # secondes


class SuperAdminOut(BaseModel):
    """Représentation d'un super-admin (sans password_hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
