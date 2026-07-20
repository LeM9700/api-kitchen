"""Tests des routes/service `delivery` zones.

Fichier place ici (Plan 01, Tache 1) pour que la commande de verification
partagee par tout le plan (`pytest tests/test_delivery.py
tests/test_delivery_zones.py`) soit executable des cette tache. Le contenu
sera principalement peuple par les taches suivantes du plan (validation
GeoJSON stricte, durcissement des routes zones, tenant slug, soft-delete,
etc.) -- aucun changement de comportement des routes n'est fait dans cette
tache.

Tache 2 (validation stricte du GeoJSON) : ces tests couvrent
`DeliveryZoneCreate.polygon` (le sous-ensemble GeoJSON Polygon strict, voir
`app.modules.delivery.common.geo`) sans dependre de la base de donnees --
`DeliveryZoneCreate` est un schema Pydantic pur, instanciable directement.
Un test HTTP de bout en bout (dernier de ce fichier) verifie separement que
l'erreur remonte bien comme un 422 avec le code metier
`INVALID_DELIVERY_POLYGON`, sans passer par les routes reelles du module
(qui exigent une session tenant + auth admin, hors scope de cette tache).
"""

import math

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.http.errors import AppError, app_error_handler
from app.modules.delivery.common.errors import InvalidDeliveryPolygonError
from app.modules.delivery.schemas import DeliveryZoneCreate

VALID_ZONE_KWARGS = {"name": "Centre-ville", "fee": 2.5}


def _square_polygon() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [2.30, 48.85],
                [2.35, 48.85],
                [2.35, 48.90],
                [2.30, 48.90],
                [2.30, 48.85],
            ]
        ],
    }


def _circle_ring(n_vertices: int, radius: float = 0.01, center: tuple[float, float] = (2.35, 48.85)) -> list[list[float]]:
    """Anneau ferme convexe (n sommets distincts + retour au premier point)."""
    cx, cy = center
    points = [
        [cx + radius * math.cos(2 * math.pi * i / n_vertices), cy + radius * math.sin(2 * math.pi * i / n_vertices)]
        for i in range(n_vertices)
    ]
    points.append(points[0])
    return points


def _make_zone(polygon: dict):
    return DeliveryZoneCreate(polygon=polygon, **VALID_ZONE_KWARGS)


def test_valid_polygon_is_accepted():
    zone = _make_zone(_square_polygon())
    assert zone.polygon["type"] == "Polygon"
    assert len(zone.polygon["coordinates"][0]) == 5


def test_polygon_exactly_501_positions_is_accepted():
    # Borne haute du plan : "entre 4 et 501 points" -- 501 doit passer.
    ring = _circle_ring(500)
    assert len(ring) == 501
    zone = _make_zone({"type": "Polygon", "coordinates": [ring]})
    assert len(zone.polygon["coordinates"][0]) == 501


def test_polygon_with_502_positions_is_rejected():
    # 502 depasse la borne haute (501) et doit etre rejete.
    ring = _circle_ring(501)
    assert len(ring) == 502
    with pytest.raises(InvalidDeliveryPolygonError) as exc_info:
        _make_zone({"type": "Polygon", "coordinates": [ring]})
    assert exc_info.value.code == "INVALID_DELIVERY_POLYGON"


def test_polygon_with_fewer_than_4_positions_is_rejected():
    ring = [[0, 0], [1, 0], [0, 0]]  # 3 positions, meme "ferme" -- trop court
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_open_ring_is_rejected():
    # Premier point != dernier point.
    ring = [[2.30, 48.85], [2.35, 48.85], [2.35, 48.90], [2.30, 48.90]]
    with pytest.raises(InvalidDeliveryPolygonError) as exc_info:
        _make_zone({"type": "Polygon", "coordinates": [ring]})
    assert exc_info.value.code == "INVALID_DELIVERY_POLYGON"
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("bad_value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_coordinate_is_rejected(bad_value):
    ring = [[2.30, 48.85], [bad_value, 48.85], [2.35, 48.90], [2.30, 48.85]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


@pytest.mark.parametrize("lat", [95, -95, 90.0001, -90.0001])
def test_out_of_range_latitude_is_rejected(lat):
    ring = [[2.30, 48.85], [2.35, lat], [2.35, 48.90], [2.30, 48.85]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


@pytest.mark.parametrize("lng", [181, -181, 180.0001])
def test_out_of_range_longitude_is_rejected(lng):
    ring = [[2.30, 48.85], [lng, 48.85], [2.35, 48.90], [2.30, 48.85]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_position_with_altitude_is_rejected():
    # [lng, lat, alt] -- 3 elements, pas 2 : refuse explicitement.
    ring = [[2.30, 48.85, 35.0], [2.35, 48.85], [2.35, 48.90], [2.30, 48.85, 35.0]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_wrong_geojson_type_is_rejected():
    ring = _square_polygon()["coordinates"]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "MultiPolygon", "coordinates": ring})


def test_hole_or_multipolygon_is_rejected():
    # 2 anneaux dans `coordinates` -- interprete comme un trou / MultiPolygon
    # implicite : hors du sous-ensemble v1 strict.
    outer = _square_polygon()["coordinates"][0]
    hole = [[2.31, 48.86], [2.32, 48.86], [2.32, 48.87], [2.31, 48.86]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [outer, hole]})


def test_degenerate_collinear_polygon_is_rejected():
    # Tous les sommets alignes -- 3 sommets distincts mais aire nulle.
    ring = [[0, 0], [1, 0], [2, 0], [0, 0]]
    with pytest.raises(InvalidDeliveryPolygonError) as exc_info:
        _make_zone({"type": "Polygon", "coordinates": [ring]})
    assert "degenere" in exc_info.value.detail or "distincts" in exc_info.value.detail


def test_degenerate_all_same_point_polygon_is_rejected():
    ring = [[5, 5], [5, 5], [5, 5], [5, 5]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_degenerate_duplicate_interior_vertex_is_rejected():
    # Sommet duplique consecutif au milieu de l'anneau (segment de longueur nulle).
    ring = [[0, 0], [1, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_self_intersecting_polygon_is_rejected():
    # Polygone "papillon" a aire signee non nulle : doit specifiquement
    # declencher la detection d'auto-intersection (pas seulement l'aire nulle).
    ring = [[0, 0], [4, 4], [4, 0], [1, 5], [0, 0]]
    with pytest.raises(InvalidDeliveryPolygonError) as exc_info:
        _make_zone({"type": "Polygon", "coordinates": [ring]})
    assert "auto-intersectant" in exc_info.value.detail


def test_non_dict_polygon_is_rejected_without_arithmetic_crash():
    # Robustesse de type : ne doit jamais lever IndexError/TypeError bruts.
    with pytest.raises(ValidationError):
        # `polygon: dict` -- pydantic rejette une liste avant meme d'appeler
        # notre field_validator ; verifie juste l'absence de crash 500.
        DeliveryZoneCreate(polygon=["not", "a", "dict"], **VALID_ZONE_KWARGS)


def test_polygon_missing_coordinates_key_is_rejected():
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon"})


def test_polygon_with_non_numeric_coordinate_is_rejected():
    ring = [[2.30, 48.85], ["oops", 48.85], [2.35, 48.90], [2.30, 48.85]]
    with pytest.raises(InvalidDeliveryPolygonError):
        _make_zone({"type": "Polygon", "coordinates": [ring]})


def test_invalid_delivery_polygon_error_is_an_app_error():
    with pytest.raises(AppError) as exc_info:
        _make_zone({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]})
    assert exc_info.value.code == "INVALID_DELIVERY_POLYGON"
    assert exc_info.value.status_code == 422
    assert exc_info.value.field == "polygon"


async def test_invalid_polygon_yields_422_business_error_over_http():
    """Verifie de bout en bout (parsing body FastAPI -> handler AppError global)
    qu'un payload invalide produit un 422 avec le code metier
    `INVALID_DELIVERY_POLYGON`, jamais un 500 ni le format de validation
    Pydantic generique. Utilise une mini-app locale (meme cablage que
    `app.main`: `add_exception_handler(AppError, app_error_handler)`) plutot
    que l'app complete, pour ne pas dependre de la DB/auth des routes reelles
    (hors scope de cette tache -- pas de changement de `router.py`).
    """
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)

    @app.post("/zones")
    def create_zone(body: DeliveryZoneCreate):
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        response = await test_client.post(
            "/zones",
            json={
                "name": "Zone test",
                "polygon": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]},
                "fee": 2.5,
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_DELIVERY_POLYGON"
    assert body["field"] == "polygon"


async def test_valid_polygon_yields_201_over_http():
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)

    @app.post("/zones", status_code=201)
    def create_zone(body: DeliveryZoneCreate):
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        response = await test_client.post(
            "/zones",
            json={
                "name": "Zone test",
                "polygon": _square_polygon(),
                "fee": 2.5,
            },
        )

    assert response.status_code == 201
