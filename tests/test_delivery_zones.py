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
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
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


# ---------------------------------------------------------------------------
# Tache 3 -- couche service (`app.modules.delivery.service`), sans passer par
# HTTP. Exercee directement contre la vraie logique (pas de mock du service
# lui-meme, contrairement a `tests/test_delivery.py` qui mocke le service
# pour isoler le router) -- seule `service.resolve_known_tenant_slug` touche
# reellement Postgres (`public.tenants`), via une session de test isolee par
# savepoint/rollback (`db_session`, voir conftest.py) pour ne rien persister.
# ---------------------------------------------------------------------------

from app.modules.delivery import service as delivery_service
from app.modules.delivery.common.errors import (
    DeliveryZoneNotFoundError,
    TenantNotFoundError,
    TenantRequiredError,
)
from app.modules.delivery.models import DeliveryZone


class _FakeSession:
    """Simule les operations `AsyncSession` utilisees par le service (`get`,
    `add`, `commit`, `refresh`) contre un dict en memoire -- evite de dependre
    de Postgres pour tester la logique pure du service (existence, idempotence).
    """

    def __init__(self, zones: dict[int, DeliveryZone] | None = None):
        self._zones = dict(zones or {})
        self._next_id = max(self._zones.keys(), default=0) + 1
        self.commit_count = 0

    async def get(self, model, zone_id):
        assert model is DeliveryZone
        return self._zones.get(zone_id)

    def add(self, obj):
        obj.id = self._next_id
        self._zones[obj.id] = obj
        self._next_id += 1

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, obj):
        pass


def _make_zone_create_body() -> DeliveryZoneCreate:
    return DeliveryZoneCreate(name="Centre-ville", polygon=_square_polygon(), fee=2.5, min_order_amount=10, estimated_minutes=30)


async def test_service_create_zone_persists_and_returns_zone():
    session = _FakeSession()
    zone = await delivery_service.create_zone(session, _make_zone_create_body())
    assert zone.id is not None
    assert zone.name == "Centre-ville"
    assert session.commit_count == 1
    assert session._zones[zone.id] is zone


async def test_service_update_zone_raises_not_found_for_missing_id():
    session = _FakeSession()
    with pytest.raises(DeliveryZoneNotFoundError):
        await delivery_service.update_zone(session, 999999, _make_zone_create_body())
    assert session.commit_count == 0


async def test_service_update_zone_updates_fields_and_commits():
    existing = DeliveryZone(id=1, name="Old", polygon=_square_polygon(), fee=1.0, min_order_amount=0, estimated_minutes=20, is_active=True)
    session = _FakeSession({1: existing})
    body = _make_zone_create_body()
    zone = await delivery_service.update_zone(session, 1, body)
    assert zone is existing
    assert zone.name == "Centre-ville"
    assert zone.fee == 2.5
    assert session.commit_count == 1


async def test_service_delete_zone_raises_not_found_for_never_existed_id():
    session = _FakeSession()
    with pytest.raises(DeliveryZoneNotFoundError):
        await delivery_service.delete_zone(session, 999999)
    assert session.commit_count == 0


async def test_service_delete_zone_sets_inactive_and_commits_once():
    existing = DeliveryZone(id=1, name="Zone", polygon=_square_polygon(), fee=1.0, min_order_amount=0, estimated_minutes=20, is_active=True)
    session = _FakeSession({1: existing})
    await delivery_service.delete_zone(session, 1)
    assert existing.is_active is False
    assert session.commit_count == 1


async def test_service_delete_zone_is_idempotent_on_already_inactive_zone():
    """Deuxieme appel sur la meme zone deja inactive : no-op reussi, pas de
    nouveau commit (l'idempotence porte sur "deja supprimee", verifiee au
    niveau du service lui-meme, pas seulement du router mocke)."""
    existing = DeliveryZone(id=1, name="Zone", polygon=_square_polygon(), fee=1.0, min_order_amount=0, estimated_minutes=20, is_active=True)
    session = _FakeSession({1: existing})

    await delivery_service.delete_zone(session, 1)
    assert session.commit_count == 1

    # Deuxieme appel : ne doit ni lever, ni re-commit, ni changer l'etat.
    await delivery_service.delete_zone(session, 1)
    assert existing.is_active is False
    assert session.commit_count == 1


async def test_resolve_known_tenant_slug_raises_required_when_header_missing():
    with pytest.raises(TenantRequiredError):
        await delivery_service.resolve_known_tenant_slug(None)


async def test_resolve_known_tenant_slug_raises_required_when_header_empty_string():
    with pytest.raises(TenantRequiredError):
        await delivery_service.resolve_known_tenant_slug("")


async def test_resolve_known_tenant_slug_raises_not_found_for_unknown_slug():
    # Requete reelle contre `public.tenants` (vide dans la DB de test) --
    # verifie la logique de la Tache 3 sans mock, pas seulement via le router.
    with pytest.raises(TenantNotFoundError):
        await delivery_service.resolve_known_tenant_slug("slug-that-does-not-exist-anywhere")


async def test_resolve_known_tenant_slug_returns_slug_for_existing_tenant(monkeypatch, db_session):
    """Insere un tenant reel (via la session isolee par savepoint/rollback de
    `db_session`) et verifie que `resolve_known_tenant_slug` le retrouve --
    en redirigeant `get_public_session` du service vers cette meme session
    isolee (meme convention que `tenant_session_override` dans
    `test_stock_phase3_integration.py`), pour que l'insertion et la lecture
    voient la meme transaction non committee."""
    await db_session.execute(
        sa.text("INSERT INTO public.tenants (slug, name, plan) VALUES (:slug, :name, :plan)"),
        {"slug": "acme-test-tenant", "name": "Acme", "plan": "starter"},
    )
    await db_session.flush()

    @asynccontextmanager
    async def _fake_get_public_session():
        yield db_session

    monkeypatch.setattr("app.modules.delivery.service.get_public_session", _fake_get_public_session)

    result = await delivery_service.resolve_known_tenant_slug("acme-test-tenant")
    assert result == "acme-test-tenant"


# ---------------------------------------------------------------------------
# Tache 4 -- controle de couverture deterministe (tie-break plus petit id
# actif). Insere deux zones actives reelles (via `db_session`, isolees par
# savepoint/rollback) dont les polygones se chevauchent sur un point commun,
# en creant explicitement la zone au plus grand id EN PREMIER, pour que le
# test ne puisse pas passer "par accident" a cause de l'ordre d'insertion
# physique en base -- seul un `ORDER BY id ASC` explicite dans
# `service.check_address` peut faire passer ce test de facon fiable.
# ---------------------------------------------------------------------------


def _overlapping_squares() -> tuple[dict, dict]:
    """Deux carres qui se chevauchent tous les deux sur le point (2.325, 48.855)."""
    zone_a = {
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
    zone_b = {
        "type": "Polygon",
        "coordinates": [
            [
                [2.31, 48.84],
                [2.36, 48.84],
                [2.36, 48.89],
                [2.31, 48.89],
                [2.31, 48.84],
            ]
        ],
    }
    return zone_a, zone_b


async def test_check_address_picks_lowest_active_zone_id_when_zones_overlap(db_session):
    overlapping_point = (48.855, 2.325)  # (lat, lng) -- dans les deux carres
    polygon_high_id, polygon_low_id = _overlapping_squares()

    # Cree la zone au plus grand id EN PREMIER (insertion volontairement dans
    # l'ordre "inverse" de l'id attendu gagnant) pour exclure un succes
    # accidentel du au seul ordre physique d'insertion.
    zone_high_id = DeliveryZone(
        id=9001,
        name="Zone grand id",
        polygon=polygon_high_id,
        fee=5.0,
        min_order_amount=0,
        estimated_minutes=45,
        is_active=True,
    )
    zone_low_id = DeliveryZone(
        id=42,
        name="Zone petit id",
        polygon=polygon_low_id,
        fee=2.0,
        min_order_amount=0,
        estimated_minutes=20,
        is_active=True,
    )
    db_session.add(zone_high_id)
    await db_session.flush()
    db_session.add(zone_low_id)
    await db_session.flush()

    winner = await delivery_service.check_address(db_session, *overlapping_point)

    assert winner.id == 42
    assert winner.name == "Zone petit id"


async def test_check_address_ignores_inactive_zone_even_with_lower_id(db_session):
    """L'ordre de tie-break ne doit s'appliquer qu'entre zones actives -- une
    zone inactive au plus petit id ne doit jamais l'emporter."""
    overlapping_point = (48.855, 2.325)
    polygon_high_id, polygon_low_id = _overlapping_squares()

    inactive_low_id = DeliveryZone(
        id=1,
        name="Zone inactive",
        polygon=polygon_low_id,
        fee=1.0,
        min_order_amount=0,
        estimated_minutes=10,
        is_active=False,
    )
    active_high_id = DeliveryZone(
        id=9002,
        name="Zone active",
        polygon=polygon_high_id,
        fee=5.0,
        min_order_amount=0,
        estimated_minutes=45,
        is_active=True,
    )
    db_session.add(inactive_low_id)
    await db_session.flush()
    db_session.add(active_high_id)
    await db_session.flush()

    winner = await delivery_service.check_address(db_session, *overlapping_point)

    assert winner.id == 9002
    assert winner.name == "Zone active"


# ---------------------------------------------------------------------------
# Tache 5 -- cas obligatoire restant du plan non couvert par les Taches 1-4 :
# "point sur bord". Aucun test existant n'exercait le ray-casting
# (`service._point_in_polygon`) sur un point situe exactement sur le contour
# d'un polygone (sommet ou milieu d'arete) -- seulement des points clairement
# a l'interieur/exterieur (Tache 4) ou les bornes lat/lng du schema Pydantic
# (Tache 3, `AddressCheckRequest`, sans rapport avec le contour d'un polygone
# donne). Fige le comportement actuel (deterministe, jamais de crash) plutot
# que de le laisser non specifie : l'algorithme de ray-casting utilise classe
# les points sur les aretes bas/gauche comme "dedans" et ceux des aretes
# haut/droite comme "dehors" (convention half-open standard de cet algorithme,
# non documentee explicitement dans le code avant ce test).
# ---------------------------------------------------------------------------


def _reference_square_polygon_ring() -> list[list[float]]:
    return [
        [2.30, 48.85],
        [2.35, 48.85],
        [2.35, 48.90],
        [2.30, 48.90],
        [2.30, 48.85],
    ]


@pytest.mark.parametrize(
    "lat,lng,expected_inside,label",
    [
        (48.85, 2.325, True, "milieu arete basse"),
        (48.85, 2.30, True, "sommet bas-gauche"),
        (48.875, 2.30, True, "milieu arete gauche"),
        (48.90, 2.325, False, "milieu arete haute"),
        (48.875, 2.35, False, "milieu arete droite"),
        (48.90, 2.35, False, "sommet haut-droit"),
    ],
)
def test_point_on_polygon_boundary_has_deterministic_result(lat, lng, expected_inside, label):
    from app.modules.delivery.service import _point_in_polygon

    result = _point_in_polygon(lat, lng, _reference_square_polygon_ring())
    assert result is expected_inside, f"{label}: attendu inside={expected_inside}, obtenu {result}"


async def test_check_address_point_exactly_on_zone_edge_matches_ray_casting(db_session):
    """Bout en bout (avec une vraie zone en base) : un point sur l'arete basse
    (convention "dedans" de l'algorithme) doit renvoyer cette zone, pas lever
    DELIVERY_ZONE_UNREACHABLE."""
    zone = DeliveryZone(
        id=777,
        name="Zone bord",
        polygon={"type": "Polygon", "coordinates": [_reference_square_polygon_ring()]},
        fee=3.0,
        min_order_amount=0,
        estimated_minutes=15,
        is_active=True,
    )
    db_session.add(zone)
    await db_session.flush()

    point_on_bottom_edge = (48.85, 2.325)  # (lat, lng)
    winner = await delivery_service.check_address(db_session, *point_on_bottom_edge)

    assert winner.id == 777
