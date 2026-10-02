from fastapi import APIRouter, Depends, Query, Request
from slowapi.util import get_remote_address

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.delivery import geocoding, service
from app.modules.delivery.schemas import (
    AddressCheckOut,
    AddressCheckRequest,
    DeliveryAvailabilityOut,
    DeliverySettingsOut,
    DeliverySettingsUpdate,
    DeliveryZoneCreate,
    DeliveryZoneDetailOut,
    DeliveryZoneOut,
    DeliveryZoneRuleIn,
    EstablishmentLocationUpdate,
    GeocodeResultOut,
    ZoneActiveUpdate,
    ZonePreviewOut,
    ZonePreviewRequest,
)

router = APIRouter()


def _user_or_ip_key(request: Request) -> str:
    """Limite par utilisateur connecte (un client derriere un NAT partage ne bloque pas les
    autres), par IP sinon."""
    user_id = getattr(request.state, "user_id", None)
    tenant_slug = getattr(request.state, "tenant_slug", None)
    if user_id:
        return f"user:{tenant_slug}:{user_id}"
    return f"ip:{get_remote_address(request)}"


def _tenant_slug_from_header(request: Request) -> str:
    return request.headers.get("X-Tenant-Slug", "default")


# --------------------------------------------------------------------------- lecture publique


@router.get("/zones", response_model=list[DeliveryZoneOut])
@limiter.limit("60/minute")
async def list_zones(request: Request, establishment_id: int | None = Query(None, ge=1)):
    """Zones actives (sans contour). Public, tenant via ``X-Tenant-Slug``."""
    async with get_tenant_session(_tenant_slug_from_header(request)) as session:
        return await service.list_zones(session, establishment_id)


@router.get("/availability", response_model=DeliveryAvailabilityOut)
@limiter.limit("60/minute")
async def get_availability(request: Request):
    """La livraison est-elle proposee, et par quels etablissements ? Public, tenant via
    ``X-Tenant-Slug``. Sert a l'app client avant d'afficher le choix livraison / retrait."""
    async with get_tenant_session(_tenant_slug_from_header(request)) as session:
        return await service.availability(session)


# --------------------------------------------------------------------------- administration


@router.get("/zones/manage", response_model=list[DeliveryZoneDetailOut])
async def list_zones_for_management(current_user=Depends(require_role("staff", "admin"))):
    """Toutes les zones (actives ou non) avec contour et regles."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_zones_for_management(session)


@router.post("/zones/preview", response_model=ZonePreviewOut)
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def preview_zone(
    request: Request, body: ZonePreviewRequest, current_user=Depends(require_role("admin"))
):
    """Calcule le contour d'une forme (cercle, temps de trajet, dessin) sans rien enregistrer."""
    return await service.preview_shape(body.shape)


@router.post("/zones", response_model=DeliveryZoneDetailOut, status_code=201)
async def create_zone(body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_zone(session, body)


@router.put("/zones/{zone_id}", response_model=DeliveryZoneDetailOut)
async def update_zone(zone_id: int, body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_zone(session, zone_id, body)


@router.patch("/zones/{zone_id}", response_model=DeliveryZoneDetailOut)
async def set_zone_active(zone_id: int, body: ZoneActiveUpdate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.set_zone_active(session, zone_id, body.is_active)


@router.delete("/zones/{zone_id}", response_model=DeliveryZoneDetailOut)
async def deactivate_zone(zone_id: int, current_user=Depends(require_role("admin"))):
    """Desactive la zone (jamais de suppression : des commandes passees y font reference)."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.set_zone_active(session, zone_id, False)


@router.put("/zones/{zone_id}/rules", response_model=DeliveryZoneDetailOut)
async def replace_zone_rules(
    zone_id: int, body: list[DeliveryZoneRuleIn], current_user=Depends(require_role("admin"))
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = await service.get_zone(session, zone_id)
        await service._replace_rules(session, zone.id, body)
        await session.commit()
        await session.refresh(zone)
        return service.zone_detail(zone, await service._zone_rules(session, zone.id))


@router.put("/establishments/{establishment_id}/location")
async def set_establishment_location(
    establishment_id: int,
    body: EstablishmentLocationUpdate,
    current_user=Depends(require_role("admin")),
):
    """Position de l'etablissement : centre des cartes et point de depart des zones."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        establishment = await service.set_establishment_location(
            session, establishment_id, body.latitude, body.longitude
        )
        return {
            "id": establishment.id,
            "latitude": establishment.latitude,
            "longitude": establishment.longitude,
        }


@router.get("/settings", response_model=DeliverySettingsOut)
async def get_settings(current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        row = await service.get_delivery_settings(session)
        await session.commit()
        return DeliverySettingsOut(
            internal_enabled=row.internal_enabled,
            driver_dispatch_enabled=row.driver_dispatch_enabled,
            delivery_proof_required=row.delivery_proof_required,
            failure_min_wait_minutes=row.failure_min_wait_minutes,
            failure_min_call_attempts=row.failure_min_call_attempts,
            version=row.version,
            updated_at=row.updated_at,
        )


@router.put("/settings", response_model=DeliverySettingsOut)
async def update_settings(
    request: Request, body: DeliverySettingsUpdate, current_user=Depends(require_role("admin"))
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        row = await service.update_delivery_settings(
            session,
            internal_enabled=body.internal_enabled,
            driver_dispatch_enabled=body.driver_dispatch_enabled,
            delivery_proof_required=body.delivery_proof_required,
            failure_min_wait_minutes=body.failure_min_wait_minutes,
            failure_min_call_attempts=body.failure_min_call_attempts,
            expected_version=body.expected_version,
            user_id=int(current_user["id"]),
            user_email=current_user.get("email"),
            ip_address=get_remote_address(request),
            user_agent=request.headers.get("user-agent"),
        )
        return DeliverySettingsOut(
            internal_enabled=row.internal_enabled,
            driver_dispatch_enabled=row.driver_dispatch_enabled,
            delivery_proof_required=row.delivery_proof_required,
            failure_min_wait_minutes=row.failure_min_wait_minutes,
            failure_min_call_attempts=row.failure_min_call_attempts,
            version=row.version,
            updated_at=row.updated_at,
        )


# --------------------------------------------------------------------------- verification d'adresse


@router.post("/check", response_model=AddressCheckOut)
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def check_address(request: Request, body: AddressCheckRequest, current_user=Depends(get_current_user)):
    """Verifie si des coordonnees GPS tombent dans une zone de livraison active et calcule les
    frais reels (regles de zone, livraison offerte) pour le sous-total fourni.

    Le geocodage (adresse texte -> lat/lng) est a la charge du client : voir ``GET /geocode``.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        if not await service.is_delivery_enabled(session):
            raise AppError("DELIVERY_DISABLED", "La livraison est momentanement indisponible", 409)
        zone = await service.check_address(session, body.lat, body.lng, body.establishment_id)
        quote = await service.quote_for_zone(session, zone, body.subtotal)
        return AddressCheckOut(
            zone_id=zone.id,
            name=zone.name,
            establishment_id=quote.establishment_id,
            fee=quote.pricing.fee,
            base_fee=quote.pricing.base_fee,
            free_delivery=quote.pricing.free_delivery,
            applied=quote.pricing.applied,
            applied_label=quote.pricing.applied_label,
            remaining_for_free=quote.pricing.remaining_for_free,
            estimated_minutes=quote.estimated_minutes,
            min_order_amount=quote.min_order_amount,
            min_order_met=quote.min_order_met,
        )


# --------------------------------------------------------------------------- geocodage


@router.get("/geocode", response_model=list[GeocodeResultOut])
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def geocode(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    lat: float | None = Query(None, ge=-90, le=90, description="Latitude pour favoriser les resultats proches"),
    lng: float | None = Query(None, ge=-180, le=180),
    language: str = Query("fr", max_length=5),
    limit: int = Query(5, ge=1, le=10),
    autocomplete: bool = True,
    current_user=Depends(get_current_user),
):
    """Adresse texte -> candidats avec coordonnees (France et Serbie), via Mapbox."""
    proximity = (lat, lng) if lat is not None and lng is not None else None
    results = await geocoding.forward_geocode(
        q, proximity=proximity, language=language, limit=limit, autocomplete=autocomplete
    )
    return [GeocodeResultOut(**result.as_dict()) for result in results]


@router.get("/reverse-geocode", response_model=GeocodeResultOut | None)
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def reverse_geocode(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    language: str = Query("fr", max_length=5),
    current_user=Depends(get_current_user),
):
    """Coordonnees -> adresse la plus proche (``null`` si aucune)."""
    result = await geocoding.reverse_geocode(lat, lng, language=language)
    return GeocodeResultOut(**result.as_dict()) if result else None


# Livreurs et dispatch comptoir (phase 3) : memes prefixe `/delivery`.
from app.modules.delivery.dispatch_router import router as _dispatch_router  # noqa: E402

router.include_router(_dispatch_router)
