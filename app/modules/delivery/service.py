import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.delivery import geocoding, geometry, pricing
from app.modules.delivery.models import (
    DeliveryZone,
    DeliveryZoneRule,
    RestaurantDeliverySettings,
    RestaurantDeliverySettingsAudit,
)
from app.modules.delivery.schemas import (
    CircleShape,
    DeliveryZoneCreate,
    DeliveryZoneDetailOut,
    DeliveryZoneRuleIn,
    DeliveryZoneRuleOut,
    IsochroneShape,
    PolygonShape,
    ZonePreviewOut,
)
from app.modules.hr.models import Establishment

logger = logging.getLogger(__name__)

# Garde-fou : la recherche de zone parcourt toutes les zones actives a chaque verification.
MAX_ACTIVE_ZONES_PER_ESTABLISHMENT = 50
MAX_RULES_PER_ZONE = 20


# Compatibilite : l'ancien nom est encore importe ici et la.
def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    return geometry.point_in_ring(lat, lng, polygon)


# --------------------------------------------------------------------------- recherche


async def _scoped_active_zones(session: AsyncSession, establishment_id: int | None) -> list[DeliveryZone]:
    stmt = select(DeliveryZone).where(DeliveryZone.is_active.is_(True))
    if establishment_id is not None:
        # Une zone sans etablissement (donnee historique) vaut pour tous les etablissements.
        stmt = stmt.where(
            (DeliveryZone.establishment_id == establishment_id) | (DeliveryZone.establishment_id.is_(None))
        )
    result = await session.execute(stmt.order_by(DeliveryZone.id))
    return list(result.scalars())


async def find_zone(
    session: AsyncSession,
    lat: float,
    lng: float,
    establishment_id: int | None = None,
) -> DeliveryZone | None:
    """Zone active qui couvre le point, ou ``None``.

    Si plusieurs zones se chevauchent (anneaux concentriques « 0-3 km » / « 0-6 km », par
    exemple), la plus petite gagne : c'est la plus specifique. A surface egale, le plus petit id.
    La regle est deterministe pour que ``/delivery/check`` et la creation de commande retiennent
    toujours la meme zone, donc les memes frais.
    """
    best: tuple[float, int] | None = None
    best_zone: DeliveryZone | None = None
    for zone in await _scoped_active_zones(session, establishment_id):
        ring = geometry.safe_ring(zone.polygon)
        if ring is None:
            logger.warning("delivery zone %s has an unreadable polygon, skipped", zone.id)
            continue
        if not geometry.point_in_ring(lat, lng, ring):
            continue
        key = (geometry.area_km2(ring), zone.id)
        if best is None or key < best:
            best, best_zone = key, zone
    return best_zone


async def check_address(
    session: AsyncSession, lat: float, lng: float, establishment_id: int | None = None
) -> DeliveryZone:
    zone = await find_zone(session, lat, lng, establishment_id)
    if zone is None:
        raise AppError("DELIVERY_ZONE_UNREACHABLE", "Adresse hors zone de livraison", 422)
    return zone


async def list_zones(session: AsyncSession, establishment_id: int | None = None) -> list[DeliveryZone]:
    stmt = select(DeliveryZone).where(DeliveryZone.is_active.is_(True))
    if establishment_id is not None:
        stmt = stmt.where(
            (DeliveryZone.establishment_id == establishment_id) | (DeliveryZone.establishment_id.is_(None))
        )
    result = await session.execute(stmt.order_by(DeliveryZone.name))
    return list(result.scalars())


# --------------------------------------------------------------------------- devis (frais)


@dataclass(frozen=True)
class DeliveryQuote:
    zone: DeliveryZone
    establishment_id: int | None
    pricing: pricing.PricingResult
    estimated_minutes: int
    min_order_amount: float
    min_order_met: bool | None


async def _establishment_for_zone(session: AsyncSession, zone: DeliveryZone) -> Establishment | None:
    if zone.establishment_id is not None:
        return await session.get(Establishment, zone.establishment_id)
    return await session.scalar(
        select(Establishment).where(Establishment.is_active.is_(True)).order_by(Establishment.id).limit(1)
    )


async def _zone_rules(session: AsyncSession, zone_id: int) -> list[DeliveryZoneRule]:
    result = await session.execute(
        select(DeliveryZoneRule).where(DeliveryZoneRule.zone_id == zone_id).order_by(DeliveryZoneRule.id)
    )
    return list(result.scalars())


async def quote_for_zone(
    session: AsyncSession,
    zone: DeliveryZone,
    subtotal: float | None,
    *,
    now: datetime | None = None,
    promo_free: bool = False,
    loyalty_free: bool = False,
) -> DeliveryQuote:
    """Frais, delai et minimum de commande d'une livraison dans ``zone``.

    Source de verite unique : utilisee par ``POST /delivery/check`` (affichage) ET par la
    creation de commande (facturation), pour que ce qui est annonce soit ce qui est facture.
    """
    establishment = await _establishment_for_zone(session, zone)
    local_now = pricing.local_now_for(establishment.timezone if establishment else None, now)
    rules = await _zone_rules(session, zone.id)
    result = pricing.evaluate_pricing(
        float(zone.fee),
        rules,
        subtotal or 0,
        local_now,
        promo_free=promo_free,
        loyalty_free=loyalty_free,
    )
    minimum = round(float(zone.min_order_amount or 0), 2)
    return DeliveryQuote(
        zone=zone,
        establishment_id=zone.establishment_id or (establishment.id if establishment else None),
        pricing=result,
        estimated_minutes=int(zone.estimated_minutes or 0),
        min_order_amount=minimum,
        min_order_met=None if subtotal is None else subtotal >= minimum,
    )


# --------------------------------------------------------------------------- formes


def _geometry_error(exc: geometry.GeometryError, field: str = "shape") -> AppError:
    return AppError(exc.code, exc.message, 422, field)


async def build_shape(shape: PolygonShape | CircleShape | IsochroneShape) -> tuple[dict, str, dict | None]:
    """Transforme une forme saisie en ``(polygone GeoJSON valide, shape_kind, shape_params)``."""
    try:
        if isinstance(shape, PolygonShape):
            return geometry.validate_geojson(shape.polygon), "polygon", None
        if isinstance(shape, CircleShape):
            ring = geometry.validate_ring(geometry.circle_ring(shape.center_lat, shape.center_lng, shape.radius_m))
            params = {"center_lat": shape.center_lat, "center_lng": shape.center_lng, "radius_m": shape.radius_m}
            return geometry.polygon_geometry(ring), "circle", params
        ring = await geocoding.isochrone_ring(shape.center_lat, shape.center_lng, shape.minutes)
        params = {"center_lat": shape.center_lat, "center_lng": shape.center_lng, "minutes": shape.minutes}
        return geometry.polygon_geometry(ring), "isochrone", params
    except geometry.GeometryError as exc:
        raise _geometry_error(exc) from exc


async def preview_shape(shape: PolygonShape | CircleShape | IsochroneShape) -> ZonePreviewOut:
    polygon, kind, params = await build_shape(shape)
    ring = polygon["coordinates"][0]
    return ZonePreviewOut(
        polygon=polygon,
        shape_kind=kind,
        shape_params=params,
        area_km2=round(geometry.area_km2(ring), 3),
        points=len(ring) - 1,
    )


# --------------------------------------------------------------------------- CRUD zones


async def _default_establishment_id(session: AsyncSession) -> int | None:
    return await session.scalar(
        select(Establishment.id).where(Establishment.is_active.is_(True)).order_by(Establishment.id).limit(1)
    )


async def _resolve_zone_establishment(session: AsyncSession, establishment_id: int | None) -> int | None:
    if establishment_id is None:
        return await _default_establishment_id(session)
    existing = await session.scalar(
        select(Establishment.id).where(Establishment.id == establishment_id, Establishment.is_active.is_(True))
    )
    if existing is None:
        raise AppError("ESTABLISHMENT_NOT_FOUND", "Etablissement introuvable ou inactif", 404, "establishment_id")
    return int(existing)


async def _assert_zone_capacity(
    session: AsyncSession, establishment_id: int | None, exclude_zone_id: int | None = None
) -> None:
    stmt = select(func.count()).select_from(DeliveryZone).where(DeliveryZone.is_active.is_(True))
    stmt = stmt.where(
        DeliveryZone.establishment_id == establishment_id
        if establishment_id is not None
        else DeliveryZone.establishment_id.is_(None)
    )
    if exclude_zone_id is not None:
        stmt = stmt.where(DeliveryZone.id != exclude_zone_id)
    if (await session.scalar(stmt) or 0) >= MAX_ACTIVE_ZONES_PER_ESTABLISHMENT:
        raise AppError(
            "ZONE_LIMIT_REACHED",
            f"Maximum {MAX_ACTIVE_ZONES_PER_ESTABLISHMENT} zones actives par etablissement",
            409,
        )


async def _replace_rules(session: AsyncSession, zone_id: int, rules: list[DeliveryZoneRuleIn]) -> None:
    if len(rules) > MAX_RULES_PER_ZONE:
        raise AppError("TOO_MANY_RULES", f"Maximum {MAX_RULES_PER_ZONE} regles par zone", 422, "rules")
    existing = await _zone_rules(session, zone_id)
    for rule in existing:
        await session.delete(rule)
    await session.flush()
    for rule in rules:
        session.add(DeliveryZoneRule(zone_id=zone_id, **rule.model_dump()))
    await session.flush()


def zone_detail(zone: DeliveryZone, rules: list[DeliveryZoneRule]) -> DeliveryZoneDetailOut:
    ring = geometry.safe_ring(zone.polygon)
    return DeliveryZoneDetailOut(
        id=zone.id,
        name=zone.name,
        establishment_id=zone.establishment_id,
        fee=float(zone.fee),
        min_order_amount=float(zone.min_order_amount or 0),
        estimated_minutes=int(zone.estimated_minutes or 0),
        is_active=bool(zone.is_active),
        polygon=zone.polygon,
        shape_kind=zone.shape_kind or "polygon",
        shape_params=zone.shape_params,
        area_km2=round(geometry.area_km2(ring), 3) if ring else 0,
        rules=[DeliveryZoneRuleOut.model_validate(rule) for rule in rules],
    )


async def list_zones_for_management(session: AsyncSession) -> list[DeliveryZoneDetailOut]:
    zones = list((await session.execute(select(DeliveryZone).order_by(DeliveryZone.name, DeliveryZone.id))).scalars())
    rules_by_zone: dict[int, list[DeliveryZoneRule]] = {}
    for rule in (await session.execute(select(DeliveryZoneRule).order_by(DeliveryZoneRule.id))).scalars():
        rules_by_zone.setdefault(rule.zone_id, []).append(rule)
    return [zone_detail(zone, rules_by_zone.get(zone.id, [])) for zone in zones]


async def get_zone(session: AsyncSession, zone_id: int) -> DeliveryZone:
    zone = await session.get(DeliveryZone, zone_id)
    if zone is None:
        raise AppError("DELIVERY_ZONE_NOT_FOUND", "Zone de livraison introuvable", 404)
    return zone


async def create_zone(session: AsyncSession, body: DeliveryZoneCreate) -> DeliveryZoneDetailOut:
    shape = body.shape or PolygonShape(kind="polygon", polygon=body.polygon or {})
    polygon, kind, params = await build_shape(shape)
    establishment_id = await _resolve_zone_establishment(session, body.establishment_id)
    if body.is_active:
        await _assert_zone_capacity(session, establishment_id)

    zone = DeliveryZone(
        name=body.name,
        establishment_id=establishment_id,
        polygon=polygon,
        shape_kind=kind,
        shape_params=params,
        fee=body.fee,
        min_order_amount=body.min_order_amount,
        estimated_minutes=body.estimated_minutes,
        is_active=body.is_active,
    )
    session.add(zone)
    await session.flush()
    if body.rules:
        await _replace_rules(session, zone.id, body.rules)
    await session.commit()
    await session.refresh(zone)
    return zone_detail(zone, await _zone_rules(session, zone.id))


async def update_zone(session: AsyncSession, zone_id: int, body: DeliveryZoneCreate) -> DeliveryZoneDetailOut:
    zone = await get_zone(session, zone_id)
    shape = body.shape or PolygonShape(kind="polygon", polygon=body.polygon or {})
    polygon, kind, params = await build_shape(shape)
    establishment_id = await _resolve_zone_establishment(
        session, body.establishment_id if body.establishment_id is not None else zone.establishment_id
    )
    if body.is_active:
        await _assert_zone_capacity(session, establishment_id, exclude_zone_id=zone.id)

    zone.name = body.name
    zone.establishment_id = establishment_id
    zone.polygon = polygon
    zone.shape_kind = kind
    zone.shape_params = params
    zone.fee = body.fee
    zone.min_order_amount = body.min_order_amount
    zone.estimated_minutes = body.estimated_minutes
    zone.is_active = body.is_active
    if body.rules is not None:
        await _replace_rules(session, zone.id, body.rules)
    await session.commit()
    await session.refresh(zone)
    return zone_detail(zone, await _zone_rules(session, zone.id))


async def set_zone_active(session: AsyncSession, zone_id: int, is_active: bool) -> DeliveryZoneDetailOut:
    """Active ou desactive une zone. Une zone n'est jamais supprimee : des commandes passees
    y font reference et on veut pouvoir la reactiver."""
    zone = await get_zone(session, zone_id)
    if is_active and not zone.is_active:
        await _assert_zone_capacity(session, zone.establishment_id, exclude_zone_id=zone.id)
    zone.is_active = is_active
    await session.commit()
    await session.refresh(zone)
    return zone_detail(zone, await _zone_rules(session, zone.id))


# --------------------------------------------------------------------------- reglages


async def get_delivery_settings(session: AsyncSession) -> RestaurantDeliverySettings:
    """Reglages de livraison du tenant (ligne unique, creee au premier acces avec les valeurs
    par defaut : livraison activee)."""
    settings_row = await session.scalar(select(RestaurantDeliverySettings).order_by(RestaurantDeliverySettings.id).limit(1))
    if settings_row is None:
        settings_row = RestaurantDeliverySettings(internal_enabled=True, version=1)
        session.add(settings_row)
        await session.flush()
        await session.refresh(settings_row)
    return settings_row


async def is_delivery_enabled(session: AsyncSession) -> bool:
    """Lecture seule (n'ecrit rien) : sans ligne de reglages, la livraison est activee."""
    value = await session.scalar(
        select(RestaurantDeliverySettings.internal_enabled).order_by(RestaurantDeliverySettings.id).limit(1)
    )
    return True if value is None else bool(value)


async def update_delivery_settings(
    session: AsyncSession,
    *,
    internal_enabled: bool,
    driver_dispatch_enabled: bool | None = None,
    expected_version: int,
    user_id: int,
    user_email: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> RestaurantDeliverySettings:
    row = await get_delivery_settings(session)
    if row.version != expected_version:
        raise AppError(
            "DELIVERY_SETTINGS_CONFLICT",
            "Les reglages ont ete modifies entre-temps, rechargez la page",
            409,
        )
    if row.internal_enabled != internal_enabled:
        session.add(
            RestaurantDeliverySettingsAudit(
                changed_by_user_id=user_id,
                user_email=user_email,
                field_name="internal_enabled",
                old_value=str(row.internal_enabled).lower(),
                new_value=str(internal_enabled).lower(),
                ip_address=(ip_address or "")[:45] or None,
                user_agent=user_agent,
            )
        )
        row.internal_enabled = internal_enabled
        row.version = row.version + 1
    if driver_dispatch_enabled is not None and row.driver_dispatch_enabled != driver_dispatch_enabled:
        if driver_dispatch_enabled:
            # Activer le dispatch sans livreur bloquerait tous les departs en livraison.
            from app.modules.delivery.models import DriverProfile

            has_driver = await session.scalar(
                select(DriverProfile.id).where(DriverProfile.is_active.is_(True)).limit(1)
            )
            if has_driver is None:
                raise AppError(
                    "NO_ACTIVE_DRIVER",
                    "Creez au moins un livreur actif avant d'activer le dispatch par livreurs.",
                    409,
                )
        session.add(
            RestaurantDeliverySettingsAudit(
                changed_by_user_id=user_id,
                user_email=user_email,
                field_name="driver_dispatch_enabled",
                old_value=str(row.driver_dispatch_enabled).lower(),
                new_value=str(driver_dispatch_enabled).lower(),
                ip_address=(ip_address or "")[:45] or None,
                user_agent=user_agent,
            )
        )
        row.driver_dispatch_enabled = driver_dispatch_enabled
        row.version = row.version + 1
    await session.commit()
    await session.refresh(row)
    return row


async def set_establishment_location(
    session: AsyncSession, establishment_id: int, latitude: float, longitude: float
) -> Establishment:
    establishment = await session.get(Establishment, establishment_id)
    if establishment is None:
        raise AppError("ESTABLISHMENT_NOT_FOUND", "Etablissement introuvable", 404)
    establishment.latitude = latitude
    establishment.longitude = longitude
    await session.commit()
    await session.refresh(establishment)
    return establishment


async def availability(session: AsyncSession) -> dict:
    enabled = await is_delivery_enabled(session)
    establishments = list(
        (
            await session.execute(
                select(Establishment).where(Establishment.is_active.is_(True)).order_by(Establishment.id)
            )
        ).scalars()
    )
    zones = await list_zones(session)
    scoped_to: set[int | None] = {zone.establishment_id for zone in zones}
    legacy_global = None in scoped_to
    return {
        "delivery_enabled": enabled and bool(zones),
        "establishments": [
            {
                "id": e.id,
                "name": e.name,
                "latitude": e.latitude,
                "longitude": e.longitude,
                "has_delivery_zones": legacy_global or e.id in scoped_to,
            }
            for e in establishments
        ],
    }
