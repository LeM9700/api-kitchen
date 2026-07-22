from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.delivery.common.geo import validate_delivery_polygon


class DeliveryZoneCreate(BaseModel):
    name: str
    polygon: dict
    fee: float = Field(..., ge=0)
    min_order_amount: float = Field(0, ge=0)
    estimated_minutes: int = Field(30, gt=0)

    @field_validator("polygon")
    @classmethod
    def _validate_polygon(cls, value: dict) -> dict:
        """Applique le sous-ensemble GeoJSON Polygon strict du module delivery.

        Leve volontairement `InvalidDeliveryPolygonError` (une `AppError`,
        pas un `ValueError`) -- voir la docstring de cette exception dans
        `app.modules.delivery.common.errors` pour le raisonnement : cela
        court-circuite l'enveloppe `pydantic.ValidationError` generique pour
        renvoyer directement le contrat d'erreur metier standard du projet
        avec le code `INVALID_DELIVERY_POLYGON`.
        """
        return validate_delivery_polygon(value)


class DeliveryZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    fee: float
    min_order_amount: float
    estimated_minutes: int
    is_active: bool


class AddressCheckRequest(BaseModel):
    """Verifie si des coordonnees GPS tombent dans une zone de livraison active.

    [P1-FF-09] Aucun geocodage n'est effectue cote API : le client (mobile/web)
    doit convertir l'adresse tapee par l'utilisateur en lat/lng avant d'appeler
    cet endpoint, via un service de geocodage externe (Google Maps Geocoding API,
    Mapbox Geocoding API, etc.). Le champ `address` ci-dessous est purement
    informatif (logs/support) et n'est pas utilise pour le calcul de zone.
    """

    lat: float = Field(..., ge=-90, le=90, description="Latitude WGS84 geocodee cote client (ex: via Google Maps/Mapbox).")
    lng: float = Field(..., ge=-180, le=180, description="Longitude WGS84 geocodee cote client (ex: via Google Maps/Mapbox).")
    address: str | None = Field(
        None,
        description="Adresse en texte libre saisie par l'utilisateur — informatif uniquement, non utilise pour le calcul.",
        max_length=512,
    )


class AddressCheckOut(BaseModel):
    """Reponse typee de `POST /delivery/check` (Tache 3).

    Meme forme JSON que le `dict` brut retourne auparavant par la route --
    `{"zone_id", "name", "fee", "estimated_minutes"}` -- pour ne pas casser
    `app-client`, qui consomme deja cet endpoint.
    """

    zone_id: int
    name: str
    fee: float
    estimated_minutes: int
