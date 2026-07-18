from pydantic import BaseModel, ConfigDict, Field


class DeliveryZoneCreate(BaseModel):
    name: str
    polygon: dict
    fee: float
    min_order_amount: float = 0
    estimated_minutes: int = 30


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

    lat: float = Field(..., description="Latitude WGS84 geocodee cote client (ex: via Google Maps/Mapbox).")
    lng: float = Field(..., description="Longitude WGS84 geocodee cote client (ex: via Google Maps/Mapbox).")
    address: str | None = Field(
        None,
        description="Adresse en texte libre saisie par l'utilisateur — informatif uniquement, non utilise pour le calcul.",
        max_length=512,
    )
