import math

import pytest

from app.modules.delivery import geometry as g

SQUARE = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]  # anti-horaire
SQUARE_CW = [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]  # horaire
BOWTIE = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]  # se croise


def _code(callable_, *args):
    with pytest.raises(g.GeometryError) as exc:
        callable_(*args)
    return exc.value.code


# --- extraction --------------------------------------------------------------


def test_extract_ring_accepts_polygon_feature_and_single_feature_collection():
    polygon = {"type": "Polygon", "coordinates": [SQUARE]}
    assert g.extract_ring(polygon) == SQUARE
    assert g.extract_ring({"type": "Feature", "geometry": polygon}) == SQUARE
    collection = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon}]}
    assert g.extract_ring(collection) == SQUARE


def test_extract_ring_refuses_unsupported_shapes():
    assert _code(g.extract_ring, {"type": "MultiPolygon", "coordinates": []}) == "GEOJSON_UNSUPPORTED"
    assert _code(g.extract_ring, {"type": "Polygon", "coordinates": [SQUARE, SQUARE]}) == "GEOJSON_UNSUPPORTED"
    two = {"type": "FeatureCollection", "features": [{}, {}]}
    assert _code(g.extract_ring, two) == "GEOJSON_UNSUPPORTED"
    assert _code(g.extract_ring, {"type": "Point", "coordinates": [0, 0]}) == "GEOJSON_INVALID"
    assert _code(g.extract_ring, "pas un objet") == "GEOJSON_INVALID"
    assert _code(g.extract_ring, {"type": "Polygon", "coordinates": []}) == "GEOJSON_INVALID"


# --- validation --------------------------------------------------------------


def test_validate_ring_closes_an_open_ring():
    ring = g.validate_ring([[0, 0], [1, 0], [1, 1], [0, 1]])
    assert ring[0] == ring[-1]
    assert len(ring) == 5


def test_validate_ring_forces_counter_clockwise_orientation():
    ring = g.validate_ring(SQUARE_CW)
    assert g._signed_area(ring[:-1]) > 0


def test_validate_ring_drops_consecutive_duplicates():
    ring = g.validate_ring([[0, 0], [0, 0], [1, 0], [1, 1], [1, 1], [0, 1], [0, 0]])
    assert len(ring) == 5


def test_validate_ring_refuses_self_intersection():
    assert _code(g.validate_ring, BOWTIE) == "POLYGON_SELF_INTERSECTING"


def test_validate_ring_refuses_too_few_points():
    assert _code(g.validate_ring, [[0, 0], [1, 1], [0, 0]]) == "POLYGON_TOO_FEW_POINTS"
    assert _code(g.validate_ring, []) == "POLYGON_TOO_FEW_POINTS"


def test_validate_ring_refuses_too_many_points():
    many = [[math.cos(t) * 0.01, math.sin(t) * 0.01] for t in [i * 2 * math.pi / 600 for i in range(600)]]
    assert _code(g.validate_ring, many) == "POLYGON_TOO_MANY_POINTS"


@pytest.mark.parametrize(
    "bad",
    [
        [[0, 0], [1, 0], [1, 91], [0, 1]],  # latitude hors bornes
        [[0, 0], [181, 0], [1, 1], [0, 1]],  # longitude hors bornes
        [[0, 0], [1, 0], [float("inf"), 1], [0, 1]],
        [[0, 0], [1, 0], [float("nan"), 1], [0, 1]],
        [[0, 0], [1, 0], ["a", 1], [0, 1]],
        [[0, 0], [1, 0], [True, 1], [0, 1]],
        [[0, 0], [1, 0], [1], [0, 1]],
        [[0, 0], [1, 0], "xx", [0, 1]],
    ],
)
def test_validate_ring_refuses_invalid_coordinates(bad):
    assert _code(g.validate_ring, bad) == "POLYGON_INVALID_COORDINATE"


def test_validate_ring_refuses_degenerate_and_huge_areas():
    assert _code(g.validate_ring, [[0, 0], [1e-7, 0], [1e-7, 1e-7], [0, 1e-7]]) == "POLYGON_TOO_SMALL"
    assert _code(g.validate_ring, [[0, 0], [40, 0], [40, 40], [0, 40]]) == "POLYGON_TOO_LARGE"
    assert _code(g.validate_ring, [[0, 0], [1, 1], [2, 2]]) == "POLYGON_TOO_SMALL"  # colineaires


def test_validate_geojson_returns_a_normalized_polygon():
    result = g.validate_geojson({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [SQUARE_CW]}})
    assert result["type"] == "Polygon"
    assert g._signed_area(result["coordinates"][0][:-1]) > 0


# --- appartenance ------------------------------------------------------------


def test_point_in_ring_uses_lng_lat_order():
    ring = [[2, 10], [4, 10], [4, 12], [2, 12], [2, 10]]  # lng 2..4, lat 10..12
    assert g.point_in_ring(lat=11, lng=3, ring=ring)
    assert not g.point_in_ring(lat=3, lng=11, ring=ring)  # lat/lng inverses
    assert not g.point_in_ring(lat=11, lng=5, ring=ring)


def test_safe_ring_never_raises_on_legacy_garbage():
    assert g.safe_ring({"type": "Polygon", "coordinates": [SQUARE]}) == [[float(a), float(b)] for a, b in SQUARE]
    assert g.safe_ring(None) is None
    assert g.safe_ring({}) is None
    assert g.safe_ring({"type": "Polygon", "coordinates": [[[0, 0]]]}) is None
    assert g.safe_ring({"type": "Polygon", "coordinates": [[["a", "b"], [1, 1], [2, 2], [0, 0]]]}) is None


# --- formes ------------------------------------------------------------------


def test_circle_ring_has_the_requested_radius_and_area():
    lat, lng, radius = 48.8566, 2.3522, 3000
    ring = g.circle_ring(lat, lng, radius)
    assert ring[0] == ring[-1] and len(ring) == 65
    assert g.point_in_ring(lat, lng, ring)
    # 100 m a l'interieur / 100 m a l'exterieur du cercle
    inside = g.destination_point(lat, lng, 90, radius - 100)
    outside = g.destination_point(lat, lng, 90, radius + 100)
    assert g.point_in_ring(inside[0], inside[1], ring)
    assert not g.point_in_ring(outside[0], outside[1], ring)
    expected = math.pi * (radius / 1000) ** 2
    assert g.area_km2(ring) == pytest.approx(expected, rel=0.02)


def test_circle_ring_refuses_out_of_range_radius():
    assert _code(g.circle_ring, 48.85, 2.35, 10) == "CIRCLE_RADIUS_OUT_OF_RANGE"
    assert _code(g.circle_ring, 48.85, 2.35, 500_000) == "CIRCLE_RADIUS_OUT_OF_RANGE"


def test_destination_point_wraps_longitude():
    lat, lng = g.destination_point(0, 179.9, 90, 50_000)
    assert -180 <= lng <= 180 and lng < 0


def test_simplify_ring_reduces_a_dense_isochrone_and_stays_valid():
    dense = g.circle_ring(48.85, 2.35, 4000, steps=2000)
    simplified = g.simplify_ring(dense, max_points=120)
    assert len(simplified) - 1 <= 120
    assert simplified[0] == simplified[-1]
    assert g.area_km2(simplified) == pytest.approx(g.area_km2(dense), rel=0.05)


def test_simplify_ring_keeps_small_rings_untouched():
    ring = g.simplify_ring(SQUARE, max_points=200)
    assert ring == g.validate_ring(SQUARE)


def test_area_and_centroid():
    ring = g.validate_ring([[2.0, 48.0], [2.1, 48.0], [2.1, 48.1], [2.0, 48.1]])
    # ~0.1 deg lng * cos(48.05) ~ 7.4 km  x  0.1 deg lat ~ 11.1 km
    assert g.area_km2(ring) == pytest.approx(7.43 * 11.12, rel=0.02)
    lat, lng = g.centroid(ring)
    assert (lat, lng) == pytest.approx((48.05, 2.05))
