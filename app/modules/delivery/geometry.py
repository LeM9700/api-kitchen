"""Geometrie des zones de livraison : validation, formes, aire, appartenance.

Code pur (aucune I/O) pour rester testable. Convention GeoJSON : une position est
``[longitude, latitude]``. Une zone est stockee comme une geometrie ``Polygon`` a un
seul anneau exterieur (pas de trous, pas de multipolygone).
"""

from __future__ import annotations

import math
from typing import Any

EARTH_RADIUS_M = 6_371_008.8

# Garde-fous contre les polygones abusifs (DoS : le ray-casting s'execute pour chaque
# zone a chaque verification d'adresse, la detection d'auto-intersection est O(n^2)).
MAX_POLYGON_POINTS = 500
MIN_POLYGON_POINTS = 3
MIN_CIRCLE_RADIUS_M = 100
MAX_CIRCLE_RADIUS_M = 50_000
# Une zone de livraison plus grande qu'un departement n'est pas une zone de livraison.
MAX_ZONE_AREA_KM2 = 20_000
MIN_ZONE_AREA_KM2 = 0.0001  # 100 m2

Ring = list[list[float]]


class GeometryError(ValueError):
    """Polygone invalide. ``code`` est un identifiant stable pour l'API et les tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Normalisation d'un GeoJSON quelconque vers un anneau
# ---------------------------------------------------------------------------


def extract_ring(geojson: Any) -> Ring:
    """Retourne l'anneau exterieur d'un GeoJSON ``Polygon``, ``Feature`` ou
    ``FeatureCollection`` d'un seul polygone, sans le valider.

    Raises:
        GeometryError: forme non supportee (MultiPolygon, trous, plusieurs polygones...).
    """
    if not isinstance(geojson, dict):
        raise GeometryError("GEOJSON_INVALID", "Le polygone doit etre un objet GeoJSON")

    kind = geojson.get("type")
    if kind == "FeatureCollection":
        features = geojson.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise GeometryError("GEOJSON_UNSUPPORTED", "Un seul polygone est autorise par zone")
        return extract_ring(features[0])
    if kind == "Feature":
        return extract_ring(geojson.get("geometry"))
    if kind == "MultiPolygon":
        raise GeometryError("GEOJSON_UNSUPPORTED", "Les multipolygones ne sont pas supportes")
    if kind != "Polygon":
        raise GeometryError("GEOJSON_INVALID", "Le GeoJSON doit etre de type Polygon")

    rings = geojson.get("coordinates")
    if not isinstance(rings, list) or not rings:
        raise GeometryError("GEOJSON_INVALID", "Le polygone n'a pas de coordonnees")
    if len(rings) > 1:
        raise GeometryError("GEOJSON_UNSUPPORTED", "Les polygones avec trous ne sont pas supportes")
    ring = rings[0]
    if not isinstance(ring, list):
        raise GeometryError("GEOJSON_INVALID", "Anneau de polygone invalide")
    return ring


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _cross(o: list[float], a: list[float], b: list[float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(a: list[float], b: list[float], p: list[float]) -> bool:
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def _segments_intersect(p1: list[float], p2: list[float], p3: list[float], p4: list[float]) -> bool:
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    if d1 == 0 and _on_segment(p3, p4, p1):
        return True
    if d2 == 0 and _on_segment(p3, p4, p2):
        return True
    if d3 == 0 and _on_segment(p1, p2, p3):
        return True
    if d4 == 0 and _on_segment(p1, p2, p4):
        return True
    return False


def _has_self_intersection(open_ring: Ring) -> bool:
    """``open_ring`` : anneau sans point de fermeture. Teste toutes les paires d'aretes
    non adjacentes."""
    n = len(open_ring)
    for i in range(n):
        a1, a2 = open_ring[i], open_ring[(i + 1) % n]
        for j in range(i + 1, n):
            # Aretes adjacentes (partagent un sommet) : normal, on les ignore.
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            b1, b2 = open_ring[j], open_ring[(j + 1) % n]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


def _signed_area(open_ring: Ring) -> float:
    total = 0.0
    n = len(open_ring)
    for i in range(n):
        x1, y1 = open_ring[i]
        x2, y2 = open_ring[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2


def area_km2(ring: Ring) -> float:
    """Aire approchee en km2 (projection equirectangulaire locale, suffisante a l'echelle
    d'une zone de livraison)."""
    open_ring = _open(ring)
    if len(open_ring) < 3:
        return 0.0
    mean_lat = sum(p[1] for p in open_ring) / len(open_ring)
    kx = math.cos(math.radians(mean_lat)) * math.pi / 180 * EARTH_RADIUS_M / 1000
    ky = math.pi / 180 * EARTH_RADIUS_M / 1000
    projected = [[p[0] * kx, p[1] * ky] for p in open_ring]
    return abs(_signed_area(projected))


def _open(ring: Ring) -> Ring:
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return ring


def validate_ring(ring: Ring) -> Ring:
    """Valide et normalise un anneau : ferme l'anneau s'il ne l'est pas, borne les
    coordonnees, limite le nombre de points, refuse l'auto-intersection, impose le
    sens anti-horaire (RFC 7946).

    Returns:
        L'anneau ferme, normalise, en ``[lng, lat]`` floats.

    Raises:
        GeometryError: avec un ``code`` stable (``POLYGON_TOO_FEW_POINTS``,
            ``POLYGON_TOO_MANY_POINTS``, ``POLYGON_INVALID_COORDINATE``,
            ``POLYGON_SELF_INTERSECTING``, ``POLYGON_TOO_SMALL``, ``POLYGON_TOO_LARGE``).
    """
    cleaned: Ring = []
    for point in ring:
        if (
            not isinstance(point, (list, tuple))
            or len(point) < 2
            or isinstance(point[0], bool)
            or isinstance(point[1], bool)
            or not isinstance(point[0], (int, float))
            or not isinstance(point[1], (int, float))
        ):
            raise GeometryError("POLYGON_INVALID_COORDINATE", "Coordonnee invalide dans le polygone")
        lng, lat = float(point[0]), float(point[1])
        if not (math.isfinite(lng) and math.isfinite(lat)):
            raise GeometryError("POLYGON_INVALID_COORDINATE", "Les coordonnees doivent etre finies")
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            raise GeometryError(
                "POLYGON_INVALID_COORDINATE",
                "Coordonnees hors limites (longitude -180..180, latitude -90..90)",
            )
        cleaned.append([lng, lat])

    # Points consecutifs identiques : ignores (un double-clic sur la carte en cree).
    deduped: Ring = []
    for point in cleaned:
        if not deduped or point != deduped[-1]:
            deduped.append(point)

    open_ring = _open(deduped)
    if len(open_ring) < MIN_POLYGON_POINTS:
        raise GeometryError("POLYGON_TOO_FEW_POINTS", "Une zone doit avoir au moins 3 points distincts")
    if len(open_ring) > MAX_POLYGON_POINTS:
        raise GeometryError(
            "POLYGON_TOO_MANY_POINTS",
            f"Une zone ne peut pas depasser {MAX_POLYGON_POINTS} points",
        )
    if _has_self_intersection(open_ring):
        raise GeometryError("POLYGON_SELF_INTERSECTING", "Le contour de la zone se croise lui-meme")

    surface = area_km2(open_ring)
    if surface < MIN_ZONE_AREA_KM2:
        raise GeometryError("POLYGON_TOO_SMALL", "La zone est trop petite")
    if surface > MAX_ZONE_AREA_KM2:
        raise GeometryError("POLYGON_TOO_LARGE", "La zone est trop grande")

    if _signed_area(open_ring) < 0:
        open_ring = list(reversed(open_ring))
    return open_ring + [open_ring[0]]


def polygon_geometry(ring: Ring) -> dict:
    return {"type": "Polygon", "coordinates": [ring]}


def validate_geojson(geojson: Any) -> dict:
    """Valide n'importe quel GeoJSON accepte par :func:`extract_ring` et retourne une
    geometrie ``Polygon`` normalisee."""
    return polygon_geometry(validate_ring(extract_ring(geojson)))


# ---------------------------------------------------------------------------
# Appartenance
# ---------------------------------------------------------------------------


def point_in_ring(lat: float, lng: float, ring: Ring) -> bool:
    """Ray-casting. ``ring`` en ``[lng, lat]``. Un point exactement sur le bord peut etre
    classe dedans ou dehors (comportement standard) ; sans importance a l'echelle GPS."""
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[0], point[1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def safe_ring(geojson: Any) -> Ring | None:
    """Anneau d'une zone *deja stockee*, ou ``None`` si la ligne est illisible (donnee
    historique non validee). Ne leve jamais : une zone corrompue ne doit pas casser la
    verification d'adresse des autres zones."""
    try:
        ring = extract_ring(geojson)
        if len(ring) < 4:
            return None
        return [[float(p[0]), float(p[1])] for p in ring]
    except (GeometryError, TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Formes
# ---------------------------------------------------------------------------


def destination_point(lat: float, lng: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Point atteint depuis (lat, lng) en suivant ``bearing_deg`` sur ``distance_m`` (sphere)."""
    phi1 = math.radians(lat)
    lambda1 = math.radians(lng)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lambda2) + 540) % 360 - 180


def circle_ring(lat: float, lng: float, radius_m: float, steps: int = 64) -> Ring:
    """Polygone regulier approchant un cercle, ferme, en ``[lng, lat]``."""
    if not (MIN_CIRCLE_RADIUS_M <= radius_m <= MAX_CIRCLE_RADIUS_M):
        raise GeometryError(
            "CIRCLE_RADIUS_OUT_OF_RANGE",
            f"Le rayon doit etre compris entre {MIN_CIRCLE_RADIUS_M} m et {MAX_CIRCLE_RADIUS_M // 1000} km",
        )
    points: Ring = []
    for step in range(steps):
        p_lat, p_lng = destination_point(lat, lng, 360 * step / steps, radius_m)
        points.append([round(p_lng, 6), round(p_lat, 6)])
    points.append(points[0])
    return points


def _perpendicular_distance(point: list[float], start: list[float], end: list[float]) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(point[0] - (start[0] + t * dx), point[1] - (start[1] + t * dy))


def _douglas_peucker(points: Ring, epsilon: float) -> Ring:
    if len(points) < 3:
        return points
    index, max_distance = 0, 0.0
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i], points[0], points[-1])
        if d > max_distance:
            index, max_distance = i, d
    if max_distance > epsilon:
        left = _douglas_peucker(points[: index + 1], epsilon)
        right = _douglas_peucker(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def simplify_ring(ring: Ring, max_points: int = 200) -> Ring:
    """Reduit un anneau (typiquement une isochrone de plusieurs milliers de points) a au
    plus ``max_points`` sommets, en augmentant la tolerance jusqu'a y arriver. Garantit
    un anneau valide (non auto-intersecte) ou, a defaut, leve ``GeometryError``."""
    open_ring = _open(ring)
    if len(open_ring) <= max_points:
        return validate_ring(open_ring)
    epsilon = 1e-5
    simplified = open_ring
    for _ in range(40):
        candidate = _douglas_peucker(open_ring + [open_ring[0]], epsilon)
        simplified = _open(candidate)
        if len(simplified) <= max_points:
            break
        epsilon *= 1.5
    return validate_ring(simplified)


def bounding_box(ring: Ring) -> tuple[float, float, float, float]:
    """(min_lat, min_lng, max_lat, max_lng)."""
    lats = [p[1] for p in ring]
    lngs = [p[0] for p in ring]
    return min(lats), min(lngs), max(lats), max(lngs)


def centroid(ring: Ring) -> tuple[float, float]:
    """(lat, lng) du centre de la boite englobante (suffisant pour centrer une carte)."""
    min_lat, min_lng, max_lat, max_lng = bounding_box(ring)
    return (min_lat + max_lat) / 2, (min_lng + max_lng) / 2
