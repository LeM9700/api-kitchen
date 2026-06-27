import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PHONE_RE = re.compile(r"^\+?[0-9\s\-]{7,20}$")


class CustomerRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str = Field(min_length=1, max_length=255)
    phone: str | None = None

    @field_validator("password")
    @classmethod
    def validate_password_policy(cls, v: str) -> str:
        """Valide la politique de complexite du mot de passe.

        Args:
            v: Mot de passe en clair soumis par l'utilisateur.

        Returns:
            Le mot de passe inchange si toutes les regles sont respectees.

        Raises:
            ValueError: Liste des regles non respectees (message clair pour le client).
        """
        missing: list[str] = []
        if len(v) < 8:
            missing.append("au moins 8 caracteres")
        if not re.search(r"[A-Z]", v):
            missing.append("au moins 1 majuscule")
        if not re.search(r"\d", v):
            missing.append("au moins 1 chiffre")
        if not re.search(r"[!@#$%^&*]", v):
            missing.append("au moins 1 caractere special parmi !@#$%^&*")
        if missing:
            raise ValueError("Mot de passe insuffisant -- manquant : " + ", ".join(missing))
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not PHONE_RE.fullmatch(v.strip()):
            raise ValueError("Numero de telephone invalide")
        return v.strip()


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None
    phone: str | None
    role: str
    email_verified: bool
    created_at: datetime


class CustomerUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not PHONE_RE.fullmatch(v.strip()):
            raise ValueError("Numero de telephone invalide")
        return v.strip()


class CustomerDeleteRequest(BaseModel):
    password: str
