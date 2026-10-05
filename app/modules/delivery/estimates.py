"""Estimations de duree de livraison (sans appel a un service d'itineraire).

Une **estimation**, pas un itineraire : distance a vol d'oiseau majoree, vitesse moyenne urbaine. Partage
entre le suivi en direct (``tracking``) et le recalcul au depart (``lifecycle``), d'ou ce module sans
dependance vers eux.
"""

from __future__ import annotations

import math

# Distance routiere ~ distance a vol d'oiseau x ROAD_FACTOR ; vitesse moyenne urbaine.
ROAD_FACTOR = 1.3
AVERAGE_SPEED_MPS = 25 / 3.6
# Temps passe a chaque arret quand plusieurs commandes partent ensemble (stationnement, remise, code).
STOP_OVERHEAD_MINUTES = 4


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def eta_minutes(lat: float, lng: float, dest_lat: float | None, dest_lng: float | None) -> int | None:
    """Minutes pour rejoindre la destination (au moins 1), ou ``None`` sans destination."""
    if dest_lat is None or dest_lng is None:
        return None
    meters = haversine_m(lat, lng, dest_lat, dest_lng) * ROAD_FACTOR
    return max(1, math.ceil(meters / AVERAGE_SPEED_MPS / 60))


def departure_minutes(
    *,
    origin: tuple[float | None, float | None],
    destination: tuple[float | None, float | None],
    zone_minutes: int | None,
    other_stops: int,
) -> int | None:
    """Duree estimee entre le depart du restaurant et la remise.

    Trajet calcule depuis la position du restaurant si on la connait (sinon duree de la zone), plus
    ``STOP_OVERHEAD_MINUTES`` par autre commande emmenee dans la meme tournee. ``None`` si rien ne permet
    d'estimer (l'estimation d'origine est alors conservee)."""
    travel: int | None = None
    if None not in origin and None not in destination:
        travel = eta_minutes(origin[0], origin[1], destination[0], destination[1])  # type: ignore[arg-type]
    if travel is None and zone_minutes:
        travel = int(zone_minutes)
    if travel is None:
        return None
    return travel + STOP_OVERHEAD_MINUTES * max(0, other_stops)
