import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class RegisterRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=64)
    tenant_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None

    @field_validator("tenant_slug")
    @classmethod
    def validate_tenant_slug(cls, v: str) -> str:
        if not TENANT_SLUG_RE.fullmatch(v):
            raise ValueError("tenant_slug must be lowercase alphanumeric with hyphens/underscores only")
        return v

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


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    tenant_slug: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    tenant_slug: str
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password_policy(cls, v: str) -> str:
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


class ChangePasswordRequest(BaseModel):
    current_password: str | None = None
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
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


class LoginRequest(BaseModel):
    tenant_slug: str
    email: EmailStr
    password: str
    mfa_code: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    session_id: int  # ID of the refresh_token row — used for GET /auth/sessions


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None
    phone: str | None
    role: str
    permissions: list[str] | None = None
    is_active: bool
    email_verified: bool
    must_change_password: bool


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_code_png_base64: str
    backup_codes: list[str]


class MfaVerifyRequest(BaseModel):
    totp_code: str | None = None
    backup_code: str | None = None

    @field_validator("totp_code", "backup_code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            raise ValueError("Code vide")
        return stripped


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip_address: str | None
    is_current: bool
