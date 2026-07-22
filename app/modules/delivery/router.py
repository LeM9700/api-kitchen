from fastapi import APIRouter, Depends, Header, Request

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.core.http.limiter import limiter
from app.modules.delivery import service
from app.modules.delivery.schemas import (
    AddressCheckOut,
    AddressCheckRequest,
    DeliveryZoneCreate,
    DeliveryZoneOut,
)

router = APIRouter()


@router.get("/zones", response_model=list[DeliveryZoneOut])
@limiter.limit("60/minute")
async def list_zones(
    request: Request,
    x_tenant_slug: str | None = Header(default=None, alias="X-Tenant-Slug"),
):
    """Liste les zones de livraison actives du tenant.

    Seule route du module qui identifie encore le tenant via un header brut
    plutot que le JWT -- voir `service.resolve_known_tenant_slug` pour le
    detail des erreurs `TENANT_REQUIRED` (400) / `TENANT_NOT_FOUND` (404).
    """
    slug = await service.resolve_known_tenant_slug(x_tenant_slug)
    async with get_tenant_session(slug) as session:
        return await service.list_zones(session)


@router.post("/zones", response_model=DeliveryZoneOut, status_code=201)
@limiter.limit("60/minute")
async def create_zone(request: Request, body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_zone(session, body)


@router.put("/zones/{zone_id}", response_model=DeliveryZoneOut)
@limiter.limit("60/minute")
async def update_zone(
    request: Request,
    zone_id: int,
    body: DeliveryZoneCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_zone(session, zone_id, body)


@router.delete("/zones/{zone_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_zone(request: Request, zone_id: int, current_user=Depends(require_role("admin"))):
    """Desactive (soft-delete) une zone de livraison. Idempotent sur une zone
    deja inactive ; 404 `DELIVERY_ZONE_NOT_FOUND` si `zone_id` n'a jamais existe.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_zone(session, zone_id)


@router.post("/check", response_model=AddressCheckOut)
@limiter.limit("30/minute")
async def check_address(request: Request, body: AddressCheckRequest, current_user=Depends(get_current_user)):
    """Verifie si des coordonnees GPS tombent dans une zone de livraison active.

    Le geocodage (adresse texte -> lat/lng) est a la charge du client — voir
    la docstring de ``AddressCheckRequest`` pour les providers recommandes.

    Rate-limite a 30/minute (plus strict que le 60/minute standard des autres
    routes `delivery`) : `docs/modules/delivery.md` signale un risque de
    reverse-engineering des zones par appels repetes. 30/minute reste large
    pour un usage normal de checkout (un client retapant son adresse
    plusieurs fois) tout en bornant davantage le probing automatise qu'une
    limite de lecture standard.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = await service.check_address(session, body.lat, body.lng)
        return AddressCheckOut(
            zone_id=zone.id,
            name=zone.name,
            fee=float(zone.fee),
            estimated_minutes=zone.estimated_minutes,
        )
