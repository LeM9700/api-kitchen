from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_public_session
from app.core.http.errors import AppError
from app.modules.delivery.common.errors import DeliveryZoneNotFoundError, TenantNotFoundError, TenantRequiredError
from app.modules.delivery.models import DeliveryZone
from app.modules.delivery.schemas import DeliveryZoneCreate


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


async def resolve_known_tenant_slug(tenant_slug: str | None) -> str:
    """Valide le tenant slug lu depuis l'en-tete `X-Tenant-Slug` de `GET /delivery/zones`.

    Seule cette route du module lit encore le tenant depuis un header brut
    (les autres passent par `current_user["tenant_slug"]` via JWT, deja fiable).
    Remplace l'ancien fallback silencieux `request.headers.get(..., "default")`
    par une verification stricte en deux temps :
      1. l'en-tete doit etre present (`TenantRequiredError`, 400) ;
      2. le slug doit correspondre a un tenant existant dans `public.tenants`
         (`TenantNotFoundError`, 404) -- meme requete que
         `app.modules.customer.service.register` / `app.modules.auth.service`.

    Args:
        tenant_slug: Valeur brute de l'en-tete `X-Tenant-Slug`, ou `None` si absent.

    Returns:
        Le slug, inchange, si le tenant existe.

    Raises:
        TenantRequiredError: Si `tenant_slug` est `None` ou vide.
        TenantNotFoundError: Si aucun tenant ne correspond au slug en base publique.
    """
    if not tenant_slug:
        raise TenantRequiredError()

    async with get_public_session() as pub:
        result = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        tenant_id = result.scalar_one_or_none()

    if tenant_id is None:
        raise TenantNotFoundError(tenant_slug)

    return tenant_slug


async def create_zone(session: AsyncSession, body: DeliveryZoneCreate) -> DeliveryZone:
    """Cree une zone de livraison. Extrait de `router.create_zone` (Tache 3) pour
    etre testable sans passer par HTTP.
    """
    zone = DeliveryZone(**body.model_dump())
    session.add(zone)
    await session.commit()
    await session.refresh(zone)
    return zone


async def update_zone(session: AsyncSession, zone_id: int, body: DeliveryZoneCreate) -> DeliveryZone:
    """Met a jour une zone de livraison existante.

    Extrait de `router.update_zone` (Tache 3) pour etre testable sans HTTP.
    Verifie explicitement l'existence de la zone avant tout `setattr` -- le
    router precedent faisait `session.get(...)` puis mutait directement,
    produisant une `AttributeError` (500) sur un id absent.

    Raises:
        DeliveryZoneNotFoundError: Si `zone_id` ne correspond a aucune zone.
    """
    zone = await session.get(DeliveryZone, zone_id)
    if zone is None:
        raise DeliveryZoneNotFoundError(zone_id)
    for key, value in body.model_dump().items():
        setattr(zone, key, value)
    await session.commit()
    await session.refresh(zone)
    return zone


async def delete_zone(session: AsyncSession, zone_id: int) -> None:
    """Desactive (soft-delete) une zone de livraison -- jamais de suppression physique.

    Idempotent sur une zone deja inactive : un second appel sur la meme zone
    existante est un no-op reussi (pas d'erreur). En revanche, un `zone_id`
    qui n'a jamais existe leve toujours `DeliveryZoneNotFoundError` -- l'idempotence
    porte sur "deja supprimee", pas sur "n'a jamais existe".

    Raises:
        DeliveryZoneNotFoundError: Si `zone_id` ne correspond a aucune zone.
    """
    zone = await session.get(DeliveryZone, zone_id)
    if zone is None:
        raise DeliveryZoneNotFoundError(zone_id)
    if zone.is_active:
        zone.is_active = False
        await session.commit()
