"""Validation stricte du sous-ensemble GeoJSON Polygon accepte par le module delivery.

Ce module valide un sous-ensemble volontairement restreint de GeoJSON Polygon
(RFC 7946, section 3.1.6), suffisant pour decrire une zone de livraison :

- ``type == "Polygon"``, un seul anneau exterieur (pas de trou, pas de
  ``MultiPolygon``) -- donc ``coordinates`` doit avoir exactement 1 element ;
- entre 4 et 501 positions dans cet anneau, premier point identique au
  dernier (anneau ferme) ;
- chaque position est exactement ``[lng, lat]`` (2 nombres finis, pas
  d'altitude), avec ``-180 <= lng <= 180`` et ``-90 <= lat <= 90`` ;
- au moins trois sommets **distincts** et une aire non nulle (un polygone
  degenere -- sommets alignes ou confondus -- est rejete) ;
- aucune auto-intersection manifeste entre segments non adjacents.

Toute violation leve `InvalidDeliveryPolygonError` (code metier
`INVALID_DELIVERY_POLYGON`, HTTP 422) -- jamais une exception arithmetique
brute (`ZeroDivisionError`, `IndexError`, `TypeError`...).

Pas de bibliotheque geometrique externe (ex: shapely/GEOS) : a l'echelle de
ce sous-ensemble (au plus 500 aretes), la verification d'auto-intersection
naive est O(n^2), soit ~125 000 comparaisons de segments dans le pire cas
(polygone a 501 positions) -- de l'ordre de quelques dizaines de
millisecondes en pur Python, ce qui reste largement suffisant pour un
garde-fou de validation d'entree et evite d'imposer une dependance native
(wheels GEOS) pour ce seul besoin.
"""

from __future__ import annotations

import math
from typing import Any

from app.modules.delivery.common.errors import InvalidDeliveryPolygonError

# Bornes du sous-ensemble GeoJSON Polygon v1 (plan reseau livreurs, tache 2) :
# un anneau ferme doit avoir au moins 4 positions (3 sommets distincts + le
# retour au premier point) et au plus 501 (borne haute fixee explicitement
# par le plan -- jusqu'a 500 sommets distincts).
MIN_RING_POSITIONS = 4
MAX_RING_POSITIONS = 501

# Bornes WGS84 standard.
LNG_MIN, LNG_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0

# Tolerance flottante pour les comparaisons d'egalite/colinearite entre
# positions. Les coordonnees sont des degres WGS84 (echelle ~1e0-1e2) :
# 1e-9 absorbe le bruit de serialisation JSON/float sans masquer un vrai
# defaut geometrique (deux points distincts a l'echelle d'une zone de
# livraison sont separes d'au moins plusieurs metres, soit >> 1e-9 degre).
_COORDINATE_EPSILON = 1e-9

# Tolerance sur l'aire (formule du lacet, en degres^2) en dessous de
# laquelle un polygone est considere degenere (sommets alignes ou
# confondus). Volontairement minuscule : le but est de detecter une aire
# nulle (bruit flottant), pas d'imposer une taille minimale de zone reelle.
_AREA_EPSILON = 1e-12

Point = tuple[float, float]


def validate_delivery_polygon(value: Any) -> dict:
    """Valide un polygone GeoJSON pour ``DeliveryZoneCreate.polygon``.

    Args:
        value: valeur brute recue (attendue: ``dict`` GeoJSON-like avec
            ``type`` et ``coordinates``).

    Returns:
        Le meme ``dict``, inchange, si le polygone est valide.

    Raises:
        InvalidDeliveryPolygonError: pour toute violation du sous-ensemble
            strict decrit dans le plan.
    """
    if not isinstance(value, dict):
        raise InvalidDeliveryPolygonError("Le polygone doit etre un objet GeoJSON (dict).")

    if value.get("type") != "Polygon":
        raise InvalidDeliveryPolygonError(
            "Le champ 'type' du polygone doit valoir 'Polygon' (pas de MultiPolygon ni d'autre geometrie)."
        )

    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        raise InvalidDeliveryPolygonError(
            "Le polygone doit avoir exactement un anneau exterieur "
            "('coordinates' doit contenir 1 element -- pas de trou, pas de MultiPolygon)."
        )

    ring = coordinates[0]
    if not isinstance(ring, list):
        raise InvalidDeliveryPolygonError("L'anneau du polygone doit etre une liste de positions.")

    if not (MIN_RING_POSITIONS <= len(ring) <= MAX_RING_POSITIONS):
        raise InvalidDeliveryPolygonError(
            f"Le polygone doit avoir entre {MIN_RING_POSITIONS} et {MAX_RING_POSITIONS} positions "
            f"(recu: {len(ring)})."
        )

    points: list[Point] = [_validate_position(position) for position in ring]

    if not _points_equal(points[0], points[-1]):
        raise InvalidDeliveryPolygonError(
            "Le premier et le dernier point de l'anneau doivent etre identiques (anneau ferme)."
        )

    edges = list(zip(points, points[1:]))
    for start, end in edges:
        if _points_equal(start, end):
            raise InvalidDeliveryPolygonError(
                "Le polygone contient un segment degenere (deux sommets consecutifs confondus)."
            )

    vertices = points[:-1]
    distinct_vertices = {_rounded(point) for point in vertices}
    if len(distinct_vertices) < 3:
        raise InvalidDeliveryPolygonError("Le polygone doit avoir au moins trois sommets distincts.")

    area = _shoelace_area(points)
    if abs(area) < _AREA_EPSILON:
        raise InvalidDeliveryPolygonError(
            "Le polygone est degenere (aire nulle -- sommets alignes ou confondus)."
        )

    if _has_self_intersection(edges):
        raise InvalidDeliveryPolygonError(
            "Le polygone est auto-intersectant (des segments non adjacents se croisent)."
        )

    return value


def _validate_position(position: Any) -> Point:
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise InvalidDeliveryPolygonError(
            "Chaque position doit etre une paire [lng, lat] (pas d'altitude, pas de type inattendu)."
        )

    lng, lat = position
    for raw in (lng, lat):
        # `bool` est une sous-classe de `int` en Python -- on le rejette
        # explicitement pour ne pas accepter silencieusement `[true, false]`.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidDeliveryPolygonError("Chaque coordonnee doit etre un nombre.")
        if not math.isfinite(raw):
            raise InvalidDeliveryPolygonError(
                "Chaque coordonnee doit etre un nombre fini (Infinity/-Infinity/NaN refuses)."
            )

    lng_f, lat_f = float(lng), float(lat)
    if not (LNG_MIN <= lng_f <= LNG_MAX):
        raise InvalidDeliveryPolygonError(
            f"La longitude doit etre comprise entre {LNG_MIN} et {LNG_MAX} (recu: {lng_f})."
        )
    if not (LAT_MIN <= lat_f <= LAT_MAX):
        raise InvalidDeliveryPolygonError(
            f"La latitude doit etre comprise entre {LAT_MIN} et {LAT_MAX} (recu: {lat_f})."
        )

    return (lng_f, lat_f)


def _points_equal(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < _COORDINATE_EPSILON and abs(a[1] - b[1]) < _COORDINATE_EPSILON


def _rounded(point: Point) -> Point:
    # Arrondi a la meme echelle que `_COORDINATE_EPSILON` pour que deux
    # positions "egales aux imprecisions flottantes pres" retombent sur la
    # meme cle dans le set de sommets distincts.
    return (round(point[0], 9), round(point[1], 9))


def _shoelace_area(ring_points: list[Point]) -> float:
    """Aire signee (formule du lacet) d'un anneau deja ferme (premier == dernier)."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring_points, ring_points[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _segment_orientation(p: Point, q: Point, r: Point) -> int:
    """Orientation du triplet (p, q, r) : 0 colineaire, 1 horaire, 2 antihoraire."""
    value = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(value) < _COORDINATE_EPSILON:
        return 0
    return 1 if value > 0 else 2


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """True si q, connu colineaire a p-r, est bien situe sur le segment [p, r]."""
    return (
        min(p[0], r[0]) - _COORDINATE_EPSILON <= q[0] <= max(p[0], r[0]) + _COORDINATE_EPSILON
        and min(p[1], r[1]) - _COORDINATE_EPSILON <= q[1] <= max(p[1], r[1]) + _COORDINATE_EPSILON
    )


def _segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Test d'intersection de segments par orientation (algorithme classique).

    Detecte a la fois les croisements francs et les cas colineaires
    (chevauchement d'un segment sur l'autre).
    """
    o1 = _segment_orientation(p1, p2, p3)
    o2 = _segment_orientation(p1, p2, p4)
    o3 = _segment_orientation(p3, p4, p1)
    o4 = _segment_orientation(p3, p4, p2)

    # Cas general (croisement franc) : exige que les 4 orientations soient
    # *strictement* non nulles. Sans ce garde, un orientation "0" issu de
    # l'epsilon de colinearite (p.ex. un point tres proche de la droite
    # portee par l'autre segment, sans y etre) serait considere comme
    # "different" de 1 ou 2 et declencherait un faux positif -- alors que
    # les cas ou une orientation vaut 0 sont deja traites explicitement
    # ci-dessous via `_on_segment`.
    if 0 not in (o1, o2, o3, o4) and o1 != o2 and o3 != o4:
        return True

    if o1 == 0 and _on_segment(p1, p3, p2):
        return True
    if o2 == 0 and _on_segment(p1, p4, p2):
        return True
    if o3 == 0 and _on_segment(p3, p1, p4):
        return True
    if o4 == 0 and _on_segment(p3, p2, p4):
        return True

    return False


def _has_self_intersection(edges: list[tuple[Point, Point]]) -> bool:
    """True si deux aretes non adjacentes du contour ferme se croisent.

    Les aretes adjacentes (qui partagent un sommet par construction, y
    compris la paire de fermeture premiere/derniere arete) sont ignorees :
    elles se "touchent" legitimement en leur sommet commun, ce n'est pas
    une auto-intersection.
    """
    edge_count = len(edges)
    for i in range(edge_count):
        # `j` demarre a `i + 2` : `i + 1` est l'arete adjacente suivante
        # (sommet partage), deliberement ignoree ci-dessous de toute facon
        # via le `continue`, mais on evite le travail inutile.
        for j in range(i + 1, edge_count):
            if j == i + 1:
                continue
            if i == 0 and j == edge_count - 1:
                continue
            if _segments_intersect(*edges[i], *edges[j]):
                return True
    return False
