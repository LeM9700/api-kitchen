# app/modules/admin/tenant_schemas.py
"""Schemas Pydantic pour le tableau de bord tenant self-service."""
import re
from datetime import date, datetime, time, timezone
from urllib.parse import urlparse

import pytz
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ── Branding — constantes de validation ──────────────────────────────────────
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
SUPPORTED_FONTS: frozenset[str] = frozenset({"inter", "poppins", "playfair_display"})
# Devises a 2 decimales uniquement — _money_to_cents() dans payments/service.py
# suppose un facteur x100 partout, les devises "zero-decimal" Stripe (JPY, etc.)
# ne sont pas supportees pour l'instant.
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"EUR", "USD", "GBP", "CAD", "CHF"})
# Locales pour lesquelles un catalogue de traduction existe (voir app/core/i18n/).
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"fr", "en"})


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_public_url(value: str | None, field_label: str) -> str | None:
    value = _blank_to_none(value)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_label} doit etre une URL http(s) valide")
    return value


class TenantConfigUpdate(BaseModel):
    """Mise a jour partielle de la configuration tenant -- tous champs optionnels."""

    is_temporarily_closed: bool | None = None
    temporary_closure_message: str | None = None
    default_closure_message: str | None = None
    prep_time_normal_minutes: int | None = Field(None, ge=1, le=180)
    prep_time_peak_minutes: int | None = Field(None, ge=1, le=360)
    peak_orders_threshold: int | None = Field(None, ge=1, le=100)
    auto_calc_prep_time: bool | None = None
    overhead_per_order_minutes: int | None = Field(None, ge=0, le=60)
    timezone: str | None = None
    large_stock_adjustment_threshold: float | None = Field(None, ge=0)
    haccp_frying_oil_enabled: bool | None = None
    currency: str | None = None
    default_language: str | None = None

    @field_validator("temporary_closure_message", "default_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v

    @field_validator("timezone", mode="before")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            pytz.timezone(v)
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"Timezone inconnue : {v!r}")
        return v

    @field_validator("currency", mode="before")
    @classmethod
    def validate_currency(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Devise non supportee : {v!r}. Valeurs autorisees : {sorted(SUPPORTED_CURRENCIES)}"
            )
        return v

    @field_validator("default_language", mode="before")
    @classmethod
    def validate_default_language(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Langue non supportee : {v!r}. Valeurs autorisees : {sorted(SUPPORTED_LANGUAGES)}"
            )
        return v


class TenantConfigResponse(BaseModel):
    """Representation complete de la configuration tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_temporarily_closed: bool
    temporary_closure_message: str | None
    default_closure_message: str
    prep_time_normal_minutes: int
    prep_time_peak_minutes: int
    peak_orders_threshold: int
    auto_calc_prep_time: bool
    overhead_per_order_minutes: int
    timezone: str
    currency: str
    default_language: str
    large_stock_adjustment_threshold: float
    print_enabled: bool = False
    print_config: dict | None = None
    haccp_frying_oil_enabled: bool = False
    updated_at: datetime
    scheduled_close_at: datetime | None


class TenantScheduledClosureRequest(BaseModel):
    scheduled_close_at: datetime | None
    temporary_closure_message: str | None = None

    @field_validator("scheduled_close_at")
    @classmethod
    def validate_scheduled_close_at(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("scheduled_close_at doit inclure une timezone")
        if v <= datetime.now(timezone.utc):
            raise ValueError("scheduled_close_at doit etre dans le futur")
        return v

    @field_validator("temporary_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class BusinessHoursCreate(BaseModel):
    slot_index: int = Field(..., ge=0, le=10)
    opens_at: time
    closes_at: time


class BusinessHoursResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_of_week: int
    slot_index: int
    opens_at: time
    closes_at: time
    is_active: bool


class ExceptionalClosureCreate(BaseModel):
    closure_date: date
    custom_message: str | None = None
    use_default_message: bool = False

    @field_validator("custom_message", mode="before")
    @classmethod
    def validate_custom_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class ExceptionalClosureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    closure_date: date
    custom_message: str | None
    use_default_message: bool
    created_at: datetime


class TenantConfigAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    changed_by_user_id: int
    user_email: str | None
    changed_at: datetime
    field_name: str
    old_value: str | None
    new_value: str | None
    ip_address: str | None


class TenantStatusResponse(BaseModel):
    is_open: bool
    estimated_prep_time_minutes: int
    message: str | None
    next_opening: str | None
    active_orders_count: int


class NextOpeningResponse(BaseModel):
    next_opening: str | None


class TenantClosureToggle(BaseModel):
    """Payload pour PATCH /tenant/toggle-closure (endpoint dédié rate-limité)."""

    is_temporarily_closed: bool
    temporary_closure_message: str | None = None

    @field_validator("temporary_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class TenantPrintConfigUpdate(BaseModel):
    print_enabled: bool | None = None
    print_config: dict | None = None


class TenantPrintConfigResponse(BaseModel):
    print_enabled: bool
    print_config: dict | None = None


class TenantSuspendRequest(BaseModel):
    suspension_message: str = Field(..., min_length=1, max_length=500)


class TenantResponse(BaseModel):
    """Représentation d'un tenant pour les endpoints super-admin."""

    id: int
    slug: str
    name: str
    plan: str
    created_at: datetime
    is_suspended: bool
    suspended_at: datetime | None
    suspension_message: str | None


# ── Branding public (Plan 02) ─────────────────────────────────────────────────


class TenantBrandingResponse(BaseModel):
    """Données de branding public — retournées sans authentification.

    [⚠️ PROD] Ne jamais ajouter de champs sensibles ici (tokens, clés, données clients).
    Ce schéma est la seule surface autorisée de GET /tenant/branding.
    """

    model_config = ConfigDict(from_attributes=True)

    display_name: str | None
    logo_url: str | None
    primary_color: str | None
    secondary_color: str | None
    font_family: str | None
    contact_phone: str | None
    contact_email: str | None
    instagram_url: str | None
    google_business_url: str | None


class TenantBrandingUpdate(BaseModel):
    """Mise à jour branding — réservée aux admins tenant (PATCH /tenant/branding).

    Tous les champs sont optionnels (patch partiel).
    """

    display_name: str | None = Field(None, max_length=120)
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    font_family: str | None = None
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None
    instagram_url: str | None = None
    google_business_url: str | None = None

    @field_validator("display_name", "contact_phone", mode="before")
    @classmethod
    def normalize_optional_text(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("primary_color", "secondary_color", mode="before")
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        """Vérifie le format #RRGGBB strict."""
        v = _blank_to_none(v)
        if v is not None and not _HEX_COLOR_RE.match(v):
            raise ValueError(f"Couleur invalide : '{v}' — format attendu #RRGGBB")
        return v

    @field_validator("logo_url", "instagram_url", "google_business_url", mode="before")
    @classmethod
    def validate_urls(cls, v: str | None, info) -> str | None:
        return _validate_public_url(v, info.field_name)

    @field_validator("contact_email", mode="before")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("font_family", mode="before")
    @classmethod
    def validate_font(cls, v: str | None) -> str | None:
        """Restreint aux fonts embarquées dans le binaire Flutter."""
        v = _blank_to_none(v)
        if v is not None and v not in SUPPORTED_FONTS:
            raise ValueError(
                f"Font non supportée : '{v}'. Valeurs autorisées : {sorted(SUPPORTED_FONTS)}"
            )
        return v
