from datetime import date, datetime, time
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- formes


class PolygonShape(BaseModel):
    """Contour dessine a la main sur la carte (GeoJSON Polygon / Feature / FeatureCollection)."""

    kind: Literal["polygon"]
    polygon: dict


class CircleShape(BaseModel):
    """Cercle de ``radius_m`` metres autour d'un point (typiquement le restaurant)."""

    kind: Literal["circle"]
    center_lat: float = Field(..., ge=-90, le=90)
    center_lng: float = Field(..., ge=-180, le=180)
    radius_m: float = Field(..., gt=0)


class IsochroneShape(BaseModel):
    """Zone atteignable en voiture en ``minutes`` depuis un point (calculee par Mapbox)."""

    kind: Literal["isochrone"]
    center_lat: float = Field(..., ge=-90, le=90)
    center_lng: float = Field(..., ge=-180, le=180)
    minutes: int = Field(..., ge=1, le=60)


ZoneShape = Annotated[Union[PolygonShape, CircleShape, IsochroneShape], Field(discriminator="kind")]


# --------------------------------------------------------------------------- regles


class DeliveryZoneRuleIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=128)
    kind: Literal["fee", "free"]
    fee: float | None = Field(None, ge=0, le=1000)
    min_subtotal: float | None = Field(None, ge=0, le=100_000)
    days_of_week: list[int] | None = None
    start_time: time | None = None
    end_time: time | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    priority: int = Field(0, ge=0, le=100)
    is_active: bool = True

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("label ne peut pas etre vide")
        return value

    @field_validator("days_of_week")
    @classmethod
    def _check_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None or not value:
            return None
        if any(isinstance(d, bool) or not 0 <= d <= 6 for d in value):
            raise ValueError("days_of_week doit contenir des entiers de 0 (lundi) a 6 (dimanche)")
        return sorted(set(value))

    @model_validator(mode="after")
    def _check_shape(self) -> "DeliveryZoneRuleIn":
        if self.kind == "fee" and self.fee is None:
            raise ValueError("fee est requis pour une regle tarifaire")
        if self.kind == "free":
            self.fee = None
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time et end_time doivent etre fournis ensemble")
        if self.start_time is not None and self.start_time == self.end_time:
            raise ValueError("start_time et end_time ne peuvent pas etre identiques")
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("ends_on doit etre posterieur a starts_on")
        return self


class DeliveryZoneRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    kind: str
    fee: float | None = None
    min_subtotal: float | None = None
    days_of_week: list[int] | None = None
    start_time: time | None = None
    end_time: time | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    priority: int = 0
    is_active: bool = True


# --------------------------------------------------------------------------- zones


class DeliveryZoneCreate(BaseModel):
    """Creation / remplacement d'une zone. Fournir ``shape`` (recommande) ou, pour les anciens
    clients, ``polygon`` seul. ``rules`` : liste complete des regles (absent = inchange)."""

    name: str = Field(..., min_length=1, max_length=128)
    establishment_id: int | None = Field(None, ge=1)
    fee: float = Field(..., ge=0, le=1000)
    min_order_amount: float = Field(0, ge=0, le=100_000)
    estimated_minutes: int = Field(30, ge=1, le=240)
    is_active: bool = True
    shape: ZoneShape | None = None
    polygon: dict | None = None
    rules: list[DeliveryZoneRuleIn] | None = Field(None, max_length=20)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name ne peut pas etre vide")
        return value

    @model_validator(mode="after")
    def _one_geometry(self) -> "DeliveryZoneCreate":
        if (self.shape is None) == (self.polygon is None):
            raise ValueError("fournir exactement un champ parmi shape et polygon")
        return self


class DeliveryZoneOut(BaseModel):
    """Vue publique (liste des zones pour l'app client) : sans le contour, pour ne pas
    cartographier la couverture du restaurant a qui le demande."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    establishment_id: int | None = None
    fee: float
    min_order_amount: float
    estimated_minutes: int
    is_active: bool


class DeliveryZoneDetailOut(DeliveryZoneOut):
    """Vue d'administration : contour, mode de saisie, surface et regles."""

    polygon: dict
    shape_kind: str = "polygon"
    shape_params: dict | None = None
    area_km2: float = 0
    rules: list[DeliveryZoneRuleOut] = Field(default_factory=list)


class ZoneActiveUpdate(BaseModel):
    is_active: bool


class ZonePreviewRequest(BaseModel):
    shape: ZoneShape


class ZonePreviewOut(BaseModel):
    polygon: dict
    shape_kind: str
    shape_params: dict | None = None
    area_km2: float
    points: int


# --------------------------------------------------------------------------- verification


class AddressCheckRequest(BaseModel):
    """Verifie si des coordonnees GPS tombent dans une zone de livraison active.

    Aucun geocodage n'est fait ici : le client convertit l'adresse en lat/lng (voir
    ``GET /delivery/geocode``). Le champ ``address`` est purement informatif.

    ``subtotal`` (sous-total du panier) permet de calculer les frais reels avec les regles de
    zone (livraison offerte des X EUR, tarif du soir...) et de verifier le minimum de commande.
    ``establishment_id`` restreint la recherche aux zones d'un etablissement ; absent, toutes
    les zones sont examinees et la reponse indique l'etablissement qui livre.
    """

    lat: float = Field(..., ge=-90, le=90, description="Latitude WGS84.")
    lng: float = Field(..., ge=-180, le=180, description="Longitude WGS84.")
    address: str | None = Field(
        None,
        description="Adresse en texte libre saisie par l'utilisateur — informatif uniquement.",
        max_length=512,
    )
    establishment_id: int | None = Field(None, ge=1)
    subtotal: float | None = Field(None, ge=0, le=100_000)


class AddressCheckOut(BaseModel):
    zone_id: int
    name: str
    establishment_id: int | None = None
    # Frais effectivement appliques au sous-total fourni (frais de base si absent).
    fee: float
    base_fee: float
    free_delivery: bool = False
    # 'rule' | None. Les promotions et recompenses sont appliquees a la creation de commande.
    applied: str | None = None
    applied_label: str | None = None
    remaining_for_free: float | None = None
    estimated_minutes: int
    min_order_amount: float
    # None si ``subtotal`` n'a pas ete fourni.
    min_order_met: bool | None = None


# --------------------------------------------------------------------------- geocodage


class GeocodeResultOut(BaseModel):
    label: str
    lat: float
    lng: float
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    country_code: str | None = None


# --------------------------------------------------------------------------- reglages


class DeliverySettingsOut(BaseModel):
    internal_enabled: bool
    # Dispatch par livreurs : `ready -> out_for_delivery` exige un livreur assigne.
    driver_dispatch_enabled: bool = False
    version: int
    updated_at: datetime | None = None


class DeliverySettingsUpdate(BaseModel):
    internal_enabled: bool
    # Absent : inchange (les anciennes apps n'envoient que `internal_enabled`).
    driver_dispatch_enabled: bool | None = None
    # Concurrence optimiste : refuse l'ecriture si quelqu'un a modifie les reglages entre-temps.
    expected_version: int = Field(..., ge=1)


class EstablishmentLocationUpdate(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class AvailabilityEstablishmentOut(BaseModel):
    id: int
    name: str
    latitude: float | None = None
    longitude: float | None = None
    has_delivery_zones: bool


class DeliveryAvailabilityOut(BaseModel):
    """Ce dont l'app client a besoin avant d'afficher le choix livraison / retrait."""

    delivery_enabled: bool
    establishments: list[AvailabilityEstablishmentOut] = Field(default_factory=list)
