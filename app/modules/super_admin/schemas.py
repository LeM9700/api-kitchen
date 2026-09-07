"""Schémas Pydantic pour le module super-admin."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class SuperAdminLoginRequest(BaseModel):
    """Corps de la requête POST /super-admin/login."""

    email: EmailStr
    password: str
    # Requis dès que le compte a mfa_enabled=True. Optionnel sinon — le login
    # délivre alors un token d'enrôlement restreint (voir service.login).
    mfa_code: str | None = None


class SuperAdminTokenResponse(BaseModel):
    """Réponse au login/refresh super-admin.

    ``refresh_token`` est absent (None) pour un token d'enrôlement MFA
    (``mfa_setup_required=True``) — ce flux restreint n'est jamais renouvelable.
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int  # secondes
    mfa_setup_required: bool = False


class SuperAdminRefreshRequest(BaseModel):
    """Corps de la requête POST /super-admin/refresh."""

    refresh_token: str


class SuperAdminMfaSetupResponse(BaseModel):
    """Réponse à POST /super-admin/mfa/setup.

    [🔒 SÉCURITÉ] ``secret`` et ``recovery_codes`` ne sont exposés en clair
    qu'une seule fois, à cet instant — jamais relogués ni renvoyés ensuite.
    """

    secret: str
    otpauth_uri: str
    qr_code_png_base64: str
    recovery_codes: list[str]


class SuperAdminMfaConfirmRequest(BaseModel):
    """Corps de la requête POST /super-admin/mfa/confirm."""

    totp_code: str


class SuperAdminOut(BaseModel):
    """Représentation d'un super-admin (sans password_hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
