from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class DeviceTokenCreate(BaseModel):
    """Payload pour enregistrer ou mettre à jour un token push.

    Attributes:
        platform: Plateforme cible — ``"ios"`` ou ``"android"``.
        token: Token APNs (iOS) ou FCM (Android).
        device_name: Nom lisible de l'appareil (optionnel, pour debug/UI).
    """

    platform: str
    token: str
    device_name: str | None = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        """Vérifie que la plateforme est ios ou android.

        Args:
            v: Valeur à valider.

        Returns:
            Valeur normalisée en minuscules.

        Raises:
            ValueError: Si la plateforme n'est pas ``"ios"`` ou ``"android"``.
        """
        normalized = v.lower()
        if normalized not in ("ios", "android"):
            raise ValueError("platform must be 'ios' or 'android'")
        return normalized

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        """Vérifie que le token n'est pas vide.

        Args:
            v: Valeur à valider.

        Returns:
            Token nettoyé (strip).

        Raises:
            ValueError: Si le token est vide après strip.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("token must not be empty")
        return stripped


class DeviceTokenResponse(BaseModel):
    """Réponse de lecture d'un token push enregistré.

    Attributes:
        id: Identifiant de l'enregistrement.
        platform: Plateforme cible.
        token: Token push (APNs ou FCM).
        device_name: Nom lisible de l'appareil.
        is_active: Indique si le token est encore valide.
        created_at: Timestamp UTC de création.
        last_used_at: Timestamp UTC du dernier push réussi, ou None.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    token: str
    device_name: str | None
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
