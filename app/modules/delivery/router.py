from fastapi import APIRouter, Depends, Request

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.modules.delivery import service
from app.modules.delivery.models import DeliveryZone
from app.modules.delivery.schemas import AddressCheckRequest, DeliveryZoneCreate, DeliveryZoneOut

router = APIRouter()


@router.get("/zones", response_model=list[DeliveryZoneOut])
async def list_zones(request: Request):
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.list_zones(session)


@router.post("/zones", response_model=DeliveryZoneOut, status_code=201)
async def create_zone(body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = DeliveryZone(**body.model_dump())
        session.add(zone)
        await session.commit()
        await session.refresh(zone)
        return zone


@router.put("/zones/{zone_id}", response_model=DeliveryZoneOut)
async def update_zone(zone_id: int, body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = await session.get(DeliveryZone, zone_id)
        for key, value in body.model_dump().items():
            setattr(zone, key, value)
        await session.commit()
        await session.refresh(zone)
        return zone


@router.post("/check")
async def check_address(body: AddressCheckRequest, current_user=Depends(get_current_user)):
    """Verifie si des coordonnees GPS tombent dans une zone de livraison active.

    Le geocodage (adresse texte -> lat/lng) est a la charge du client — voir
    la docstring de ``AddressCheckRequest`` pour les providers recommandes.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = await service.check_address(session, body.lat, body.lng)
        return {
            "zone_id": zone.id,
            "name": zone.name,
            "fee": float(zone.fee),
            "estimated_minutes": zone.estimated_minutes,
        }
