from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.delivery.models import DeliveryZone


def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, point in enumerate(polygon):
        xi, yi = point
        xj, yj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


async def check_address(session: AsyncSession, lat: float, lng: float) -> DeliveryZone:
    result = await session.execute(select(DeliveryZone).where(DeliveryZone.is_active.is_(True)))
    for zone in result.scalars():
        coords = zone.polygon.get("coordinates", [[]])[0]
        if coords and _point_in_polygon(lat, lng, coords):
            return zone
    raise AppError("DELIVERY_ZONE_UNREACHABLE", "Adresse hors zone de livraison", 422)


async def list_zones(session: AsyncSession) -> list[DeliveryZone]:
    result = await session.execute(select(DeliveryZone).where(DeliveryZone.is_active.is_(True)).order_by(DeliveryZone.name))
    return list(result.scalars())
