"""Proxy de geocodage Mapbox : parsing, cache, erreurs, jeton jamais journalise.

Aucun appel reseau : le client httpx est remplace par un ``MockTransport``.
"""
import logging

import httpx
import pytest

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.delivery import geocoding, geometry

TOKEN = "sk.secret-token-for-tests"

FORWARD = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [2.3522, 48.8566]},
            "properties": {
                "name": "12 Rue de Rivoli",
                "full_address": "12 Rue de Rivoli, 75004 Paris, France",
                "place_formatted": "75004 Paris, France",
                "coordinates": {"latitude": 48.8566, "longitude": 2.3522},
                "context": {
                    "postcode": {"name": "75004"},
                    "place": {"name": "Paris"},
                    "country": {"country_code": "FR"},
                },
            },
        },
        # Sans coordonnees exploitables : ignore.
        {"geometry": {}, "properties": {"name": "Sans point", "full_address": "x"}},
        # Coordonnees hors bornes : ignore.
        {"properties": {"name": "Hors bornes", "full_address": "y", "coordinates": {"latitude": 99, "longitude": 2}}},
        # Coordonnees seulement dans la geometrie (lng, lat).
        {
            "geometry": {"type": "Point", "coordinates": [20.4489, 44.7866]},
            "properties": {"name": "Knez Mihailova", "place_formatted": "Beograd, Srbija"},
        },
    ]
}


class Recorder:
    def __init__(self, payload=None, status=200):
        self.payload = FORWARD if payload is None else payload
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


@pytest.fixture
def mapbox(monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", TOKEN)
    monkeypatch.setattr(settings, "mapbox_geocoding_permanent", False)
    geocoding.clear_cache()
    recorder = Recorder()
    monkeypatch.setattr(
        geocoding,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler)),
    )
    yield recorder
    geocoding.clear_cache()


async def test_forward_geocode_parses_and_filters_results(mapbox):
    results = await geocoding.forward_geocode("12 rue de rivoli", proximity=(48.85, 2.35))

    assert [r.label for r in results] == [
        "12 Rue de Rivoli, 75004 Paris, France",
        "Knez Mihailova, Beograd, Srbija",
    ]
    first = results[0]
    assert (first.lat, first.lng) == (48.8566, 2.3522)
    assert (first.postcode, first.city, first.country_code) == ("75004", "Paris", "fr")
    assert (results[1].lat, results[1].lng) == (44.7866, 20.4489)  # lng/lat de la geometrie, ordre corrige


async def test_forward_geocode_sends_expected_parameters(mapbox):
    await geocoding.forward_geocode("rue de rivoli", proximity=(48.85, 2.35), countries=["FR", "xx"], language="sr")

    params = dict(mapbox.requests[0].url.params)
    assert params["access_token"] == TOKEN
    assert params["q"] == "rue de rivoli"
    assert params["country"] == "fr"  # "xx" filtre : seuls France et Serbie sont autorises
    assert params["language"] == "sr"
    assert params["proximity"] == "2.35,48.85"  # Mapbox attend lng,lat
    assert params["autocomplete"] == "true"
    assert "permanent" not in params


async def test_permanent_flag_is_only_sent_when_enabled_and_not_autocompleting(mapbox, monkeypatch):
    monkeypatch.setattr(settings, "mapbox_geocoding_permanent", True)
    await geocoding.forward_geocode("rue de rivoli", autocomplete=True)
    await geocoding.forward_geocode("rue de rivoli finale", autocomplete=False)
    reverse = await geocoding.reverse_geocode(48.8566, 2.3522)

    assert "permanent" not in dict(mapbox.requests[0].url.params)
    assert dict(mapbox.requests[1].url.params)["permanent"] == "true"
    assert dict(mapbox.requests[2].url.params)["permanent"] == "true"
    assert reverse is not None


async def test_unknown_language_and_countries_fall_back_to_defaults(mapbox):
    await geocoding.forward_geocode("rue de rivoli", countries=["de"], language="klingon")
    params = dict(mapbox.requests[0].url.params)
    assert params["country"] == "fr,rs"
    assert params["language"] == "fr"


async def test_identical_queries_are_served_from_cache(mapbox):
    await geocoding.forward_geocode("rue de rivoli", proximity=(48.8501, 2.3499))
    await geocoding.forward_geocode("  Rue   de Rivoli ", proximity=(48.8504, 2.3501))  # meme requete normalisee
    assert len(mapbox.requests) == 1

    await geocoding.forward_geocode("rue de rivoli", proximity=(10.0, 10.0))  # autre proximite
    assert len(mapbox.requests) == 2


async def test_reverse_geocode_returns_first_result_or_none(mapbox):
    result = await geocoding.reverse_geocode(48.8566, 2.3522)
    assert result is not None and result.city == "Paris"
    assert dict(mapbox.requests[0].url.params)["latitude"] == "48.8566"

    mapbox.payload = {"features": []}
    geocoding.clear_cache()
    assert await geocoding.reverse_geocode(1.0, 1.0) is None


@pytest.mark.parametrize("query,code", [("ab", "GEOCODING_QUERY_TOO_SHORT"), ("  ", "GEOCODING_QUERY_TOO_SHORT"), ("x" * 201, "GEOCODING_QUERY_TOO_LONG")])
async def test_query_length_is_validated_before_any_request(mapbox, query, code):
    with pytest.raises(AppError) as exc:
        await geocoding.forward_geocode(query)
    assert exc.value.code == code and exc.value.status_code == 422
    assert mapbox.requests == []


async def test_missing_token_is_a_503_without_any_request(monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", "")
    geocoding.clear_cache()
    with pytest.raises(AppError) as exc:
        await geocoding.forward_geocode("rue de rivoli")
    assert exc.value.code == "GEOCODING_NOT_CONFIGURED" and exc.value.status_code == 503


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_upstream_errors_become_503_and_are_not_cached(mapbox, status):
    mapbox.status = status
    with pytest.raises(AppError) as exc:
        await geocoding.forward_geocode("rue de rivoli")
    assert exc.value.code == "GEOCODING_UNAVAILABLE" and exc.value.status_code == 503

    mapbox.status = 200
    assert len(await geocoding.forward_geocode("rue de rivoli")) == 2  # pas de cache d'erreur


async def test_network_failure_never_leaks_the_token_in_logs(monkeypatch, caplog):
    monkeypatch.setattr(settings, "mapbox_access_token", TOKEN)
    geocoding.clear_cache()

    def boom(request):
        raise httpx.ConnectError(f"failed to reach {request.url}")  # le message contient l'URL, donc le jeton

    monkeypatch.setattr(
        geocoding, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AppError) as exc:
            await geocoding.forward_geocode("rue de rivoli")
    assert exc.value.code == "GEOCODING_UNAVAILABLE"
    assert TOKEN not in caplog.text
    assert TOKEN not in exc.value.detail


async def test_httpx_logger_is_kept_quiet_so_urls_with_tokens_are_not_logged():
    assert logging.getLogger("httpx").level >= logging.WARNING


async def test_non_json_upstream_body_is_a_503(monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", TOKEN)
    geocoding.clear_cache()
    monkeypatch.setattr(
        geocoding,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))),
    )
    with pytest.raises(AppError) as exc:
        await geocoding.forward_geocode("rue de rivoli")
    assert exc.value.code == "GEOCODING_UNAVAILABLE"


# --- isochrone ---------------------------------------------------------------


def _isochrone_payload(points: int = 1500):
    ring = geometry.circle_ring(48.85, 2.35, 4000, steps=points)
    return {"features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


async def test_isochrone_ring_is_simplified_and_valid(mapbox):
    mapbox.payload = _isochrone_payload()
    ring = await geocoding.isochrone_ring(48.85, 2.35, 15)

    assert ring[0] == ring[-1] and len(ring) - 1 <= 200
    request = mapbox.requests[0]
    assert "/driving/2.35,48.85" in request.url.path  # Mapbox : lng,lat
    assert dict(request.url.params)["contours_minutes"] == "15"


@pytest.mark.parametrize("minutes", [0, 61, -5])
async def test_isochrone_minutes_are_bounded(mapbox, minutes):
    with pytest.raises(AppError) as exc:
        await geocoding.isochrone_ring(48.85, 2.35, minutes)
    assert exc.value.code == "ISOCHRONE_MINUTES_OUT_OF_RANGE"
    assert mapbox.requests == []


async def test_unusable_isochrone_answer_is_a_clean_422(mapbox):
    mapbox.payload = {"features": []}
    with pytest.raises(AppError) as exc:
        await geocoding.isochrone_ring(48.85, 2.35, 10)
    assert exc.value.code == "ISOCHRONE_UNAVAILABLE" and exc.value.status_code == 422

    mapbox.payload = {"features": [{"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []}}]}
    with pytest.raises(AppError) as exc:
        await geocoding.isochrone_ring(48.85, 2.35, 10)
    assert exc.value.code == "ISOCHRONE_UNAVAILABLE"
