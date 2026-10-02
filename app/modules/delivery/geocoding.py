"""Geocodage d'adresses et isochrones via Mapbox, derriere un proxy serveur.

Pourquoi un proxy : le jeton Mapbox secret reste cote serveur, le trafic est limite par
utilisateur (cout), les reponses identiques sont mises en cache, et on peut changer de
fournisseur sans republier les apps.

Les journaux ``httpx`` affichent l'URL complete des requetes, jeton compris : leur niveau est
remonte a WARNING pour ne jamais ecrire le jeton Mapbox dans les logs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.delivery import geometry

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6"
ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox"

# Pays autorises : France et Serbie (restaurants enregistres a ce jour).
ALLOWED_COUNTRIES = ("fr", "rs")
TIMEOUT_SECONDS = 6.0
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 512
MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 200
MAX_ISOCHRONE_MINUTES = 60
SUPPORTED_LANGUAGES = {"fr", "en", "sr"}


@dataclass(frozen=True)
class GeocodeResult:
    label: str
    lat: float
    lng: float
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    country_code: str | None = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "lat": self.lat,
            "lng": self.lng,
            "street": self.street,
            "postcode": self.postcode,
            "city": self.city,
            "country_code": self.country_code,
        }


# Cache en memoire du process : evite de refacturer la meme frappe repetee (autocompletion).
_cache: dict[tuple, tuple[float, list[GeocodeResult]]] = {}


def clear_cache() -> None:
    _cache.clear()


# Remplacable dans les tests : retourne un client httpx (par exemple avec MockTransport).
def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


def _token() -> str:
    token = settings.mapbox_access_token
    if not token:
        raise AppError(
            "GEOCODING_NOT_CONFIGURED",
            "Le geocodage n'est pas configure sur ce serveur",
            503,
        )
    return token


async def _get_json(url: str, params: dict) -> dict:
    params = {**params, "access_token": _token()}
    try:
        async with _client_factory() as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        # str(exc) peut contenir l'URL (donc le jeton) : on ne journalise que le type.
        logger.warning("mapbox request failed: %s", type(exc).__name__)
        raise AppError("GEOCODING_UNAVAILABLE", "Service de geocodage indisponible", 503) from exc
    if response.status_code != 200:
        logger.warning("mapbox returned HTTP %s", response.status_code)
        raise AppError("GEOCODING_UNAVAILABLE", "Service de geocodage indisponible", 503)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AppError("GEOCODING_UNAVAILABLE", "Service de geocodage indisponible", 503) from exc
    return payload if isinstance(payload, dict) else {}


def _clean_countries(countries: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not countries:
        return ALLOWED_COUNTRIES
    cleaned = tuple(c for c in (x.strip().lower() for x in countries) if c in ALLOWED_COUNTRIES)
    return cleaned or ALLOWED_COUNTRIES


def _name(context: dict, key: str) -> str | None:
    value = context.get(key)
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    return None


def _parse_feature(feature: dict) -> GeocodeResult | None:
    properties = feature.get("properties") or {}
    coords = properties.get("coordinates") or {}
    lat, lng = coords.get("latitude"), coords.get("longitude")
    if lat is None or lng is None:
        geometry_coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(geometry_coords) >= 2:
            lng, lat = geometry_coords[0], geometry_coords[1]
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None

    context = properties.get("context") or {}
    street = properties.get("name_preferred") or properties.get("name")
    label = properties.get("full_address") or ", ".join(
        part for part in (street, properties.get("place_formatted")) if part
    )
    if not label:
        return None
    country = context.get("country") if isinstance(context.get("country"), dict) else {}
    country_code = (country.get("country_code") or "").lower() or None
    return GeocodeResult(
        label=str(label),
        lat=float(lat),
        lng=float(lng),
        street=str(street) if street else None,
        postcode=_name(context, "postcode"),
        city=_name(context, "place"),
        country_code=country_code,
    )


def _cache_get(key: tuple) -> list[GeocodeResult] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: tuple, value: list[GeocodeResult]) -> None:
    if len(_cache) >= CACHE_MAX_ENTRIES:
        # Eviction simple : on repart d'un cache vide plutot que de grossir sans limite.
        _cache.clear()
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)


def _language(language: str | None) -> str:
    return language if language in SUPPORTED_LANGUAGES else "fr"


async def forward_geocode(
    query: str,
    *,
    proximity: tuple[float, float] | None = None,
    countries: list[str] | tuple[str, ...] | None = None,
    language: str | None = "fr",
    limit: int = 5,
    autocomplete: bool = True,
) -> list[GeocodeResult]:
    """Adresse texte -> candidats ``(label, lat, lng, ...)``. ``proximity`` = (lat, lng) pour
    favoriser les resultats proches du restaurant."""
    text = " ".join((query or "").split())
    if len(text) < MIN_QUERY_LENGTH:
        raise AppError("GEOCODING_QUERY_TOO_SHORT", "Saisissez au moins 3 caracteres", 422, "q")
    if len(text) > MAX_QUERY_LENGTH:
        raise AppError("GEOCODING_QUERY_TOO_LONG", "Adresse trop longue", 422, "q")
    limit = max(1, min(int(limit), 10))
    wanted = _clean_countries(countries)
    lang = _language(language)
    proximity_key = (round(proximity[0], 2), round(proximity[1], 2)) if proximity else None
    key = ("fwd", text.lower(), proximity_key, wanted, lang, limit, autocomplete)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    params: dict = {
        "q": text,
        "country": ",".join(wanted),
        "language": lang,
        "limit": limit,
        "autocomplete": "true" if autocomplete else "false",
        "types": "address,street,place,locality,neighborhood,postcode",
    }
    if proximity:
        params["proximity"] = f"{proximity[1]},{proximity[0]}"  # Mapbox : lng,lat
    if settings.mapbox_geocoding_permanent and not autocomplete:
        params["permanent"] = "true"

    payload = await _get_json(f"{GEOCODE_URL}/forward", params)
    results = [r for r in (_parse_feature(f) for f in payload.get("features") or []) if r is not None]
    _cache_put(key, results)
    return results


async def reverse_geocode(lat: float, lng: float, *, language: str | None = "fr") -> GeocodeResult | None:
    """Coordonnees -> adresse la plus proche (ou ``None``)."""
    lang = _language(language)
    key = ("rev", round(lat, 5), round(lng, 5), lang)
    cached = _cache_get(key)
    if cached is not None:
        return cached[0] if cached else None

    params: dict = {
        "latitude": lat,
        "longitude": lng,
        "language": lang,
        "country": ",".join(ALLOWED_COUNTRIES),
        "limit": 1,
    }
    if settings.mapbox_geocoding_permanent:
        params["permanent"] = "true"
    payload = await _get_json(f"{GEOCODE_URL}/reverse", params)
    results = [r for r in (_parse_feature(f) for f in payload.get("features") or []) if r is not None]
    _cache_put(key, results)
    return results[0] if results else None


async def isochrone_ring(lat: float, lng: float, minutes: int, *, max_points: int = 200) -> geometry.Ring:
    """Contour (anneau ferme ``[lng, lat]``) des points atteignables en voiture en
    ``minutes`` depuis (lat, lng), simplifie a ``max_points`` sommets maximum."""
    if not 1 <= minutes <= MAX_ISOCHRONE_MINUTES:
        raise AppError(
            "ISOCHRONE_MINUTES_OUT_OF_RANGE",
            f"Le temps de trajet doit etre compris entre 1 et {MAX_ISOCHRONE_MINUTES} minutes",
            422,
        )
    payload = await _get_json(
        f"{ISOCHRONE_URL}/driving/{lng},{lat}",
        {"contours_minutes": minutes, "polygons": "true", "denoise": 1, "generalize": 50},
    )
    try:
        ring = geometry.extract_ring((payload.get("features") or [])[0])
        return geometry.simplify_ring(ring, max_points=max_points)
    except (IndexError, geometry.GeometryError) as exc:
        logger.warning("mapbox isochrone unusable: %s", type(exc).__name__)
        raise AppError(
            "ISOCHRONE_UNAVAILABLE",
            "Impossible de calculer la zone par temps de trajet pour ce point",
            422,
        ) from exc
