"""Phase 1 livraison : zones par etablissement, formes, regles de frais, reglages, commandes.

Tests en base (savepoint annule en fin de test), appels directs au service.
"""
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.delivery import geocoding, geometry
from app.modules.delivery import service as delivery_service
from app.modules.delivery.schemas import (
    CircleShape,
    DeliveryZoneCreate,
    DeliveryZoneRuleIn,
    IsochroneShape,
    PolygonShape,
)

# Paris centre (lat 48.8566, lng 2.3522). Cercles de 2 km et 6 km : anneaux concentriques.
CENTER = (48.8566, 2.3522)
SQUARE = {"type": "Polygon", "coordinates": [[[2.30, 48.80], [2.40, 48.80], [2.40, 48.90], [2.30, 48.90], [2.30, 48.80]]]}


async def _establishments(session, count=2):
    from app.modules.hr.models import Establishment

    await session.execute(sa.text("UPDATE establishments SET is_active = false"))
    await session.execute(sa.text("UPDATE delivery_zones SET is_active = false"))
    created = []
    for index in range(count):
        est = Establishment(name=f"Resto {index}", timezone="Europe/Paris", is_active=True)
        session.add(est)
        created.append(est)
    await session.flush()
    return [e.id for e in created]


def _circle(radius_m, name="Zone", fee=3, establishment_id=None, **extra):
    return DeliveryZoneCreate(
        name=name,
        establishment_id=establishment_id,
        fee=fee,
        estimated_minutes=25,
        shape=CircleShape(kind="circle", center_lat=CENTER[0], center_lng=CENTER[1], radius_m=radius_m),
        **extra,
    )


async def _product(session):
    from app.modules.catalog.models import Product

    product = Product(name="Pizza P1", base_price=12, is_active=True)
    session.add(product)
    await session.flush()
    return product


def _delivery_body(product, establishment_id=None, lat=CENTER[0], lng=CENTER[1], quantity=1, **extra):
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate

    return OrderCreate(
        order_type="delivery",
        establishment_id=establishment_id,
        delivery_address="1 rue de Rivoli",
        customer_phone="06 12 34 56 78",
        delivery_lat=lat,
        delivery_lng=lng,
        items=[OrderItemCreate(product_id=product.id, quantity=quantity)],
        **extra,
    )


# --------------------------------------------------------------------------- creation / formes


async def test_create_circle_zone_stores_a_valid_polygon_and_its_parameters(db_session):
    est_a, _ = await _establishments(db_session)
    zone = await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est_a))

    assert zone.shape_kind == "circle"
    assert zone.shape_params == {"center_lat": CENTER[0], "center_lng": CENTER[1], "radius_m": 3000}
    assert zone.establishment_id == est_a
    assert zone.polygon["type"] == "Polygon"
    assert zone.area_km2 == pytest.approx(3.14159 * 9, rel=0.03)
    assert geometry.point_in_ring(CENTER[0], CENTER[1], zone.polygon["coordinates"][0])


async def test_zone_defaults_to_the_first_active_establishment(db_session):
    est_a, _est_b = await _establishments(db_session)
    zone = await delivery_service.create_zone(db_session, _circle(1000))
    assert zone.establishment_id == est_a


async def test_unknown_or_inactive_establishment_is_refused(db_session):
    await _establishments(db_session)
    with pytest.raises(AppError) as exc:
        await delivery_service.create_zone(db_session, _circle(1000, establishment_id=999_999))
    assert exc.value.code == "ESTABLISHMENT_NOT_FOUND"


async def test_legacy_polygon_field_and_feature_collection_are_still_accepted(db_session):
    await _establishments(db_session)
    legacy = DeliveryZoneCreate(
        name="Ancienne forme",
        fee=2,
        polygon={"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": SQUARE}]},
    )
    zone = await delivery_service.create_zone(db_session, legacy)
    assert zone.shape_kind == "polygon" and zone.polygon["type"] == "Polygon"


async def test_self_intersecting_polygon_is_refused_with_a_stable_code(db_session):
    await _establishments(db_session)
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    with pytest.raises(AppError) as exc:
        await delivery_service.create_zone(
            db_session, DeliveryZoneCreate(name="Noeud", fee=1, shape=PolygonShape(kind="polygon", polygon=bowtie))
        )
    assert exc.value.code == "POLYGON_SELF_INTERSECTING" and exc.value.status_code == 422


def test_create_schema_requires_exactly_one_geometry():
    with pytest.raises(ValidationError):
        DeliveryZoneCreate(name="x", fee=1)
    with pytest.raises(ValidationError):
        DeliveryZoneCreate(
            name="x",
            fee=1,
            polygon=SQUARE,
            shape=CircleShape(kind="circle", center_lat=1, center_lng=1, radius_m=500),
        )


@pytest.mark.parametrize("radius", [50, 80_000])
async def test_circle_radius_is_bounded(db_session, radius):
    await _establishments(db_session)
    with pytest.raises(AppError) as exc:
        await delivery_service.create_zone(db_session, _circle(radius))
    assert exc.value.code == "CIRCLE_RADIUS_OUT_OF_RANGE"


async def test_isochrone_zone_uses_the_provider_and_records_the_minutes(db_session, monkeypatch):
    await _establishments(db_session)
    monkeypatch.setattr(settings, "mapbox_access_token", "tok")
    geocoding.clear_cache()
    ring = geometry.circle_ring(CENTER[0], CENTER[1], 4000, steps=1200)
    payload = {"features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
    monkeypatch.setattr(
        geocoding,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))),
    )

    preview = await delivery_service.preview_shape(
        IsochroneShape(kind="isochrone", center_lat=CENTER[0], center_lng=CENTER[1], minutes=12)
    )
    assert preview.shape_kind == "isochrone" and preview.points <= 200
    assert preview.shape_params["minutes"] == 12

    zone = await delivery_service.create_zone(
        db_session,
        DeliveryZoneCreate(
            name="12 min",
            fee=4,
            shape=IsochroneShape(kind="isochrone", center_lat=CENTER[0], center_lng=CENTER[1], minutes=12),
        ),
    )
    assert zone.shape_kind == "isochrone" and zone.shape_params["minutes"] == 12


async def test_preview_does_not_persist_anything(db_session):
    await _establishments(db_session)
    before = await db_session.scalar(sa.text("SELECT count(*) FROM delivery_zones"))
    preview = await delivery_service.preview_shape(
        CircleShape(kind="circle", center_lat=CENTER[0], center_lng=CENTER[1], radius_m=1500)
    )
    assert preview.area_km2 > 6 and preview.points == 64
    assert await db_session.scalar(sa.text("SELECT count(*) FROM delivery_zones")) == before


# --------------------------------------------------------------------------- recherche de zone


async def test_smallest_zone_wins_among_concentric_zones(db_session):
    est, _ = await _establishments(db_session)
    outer = await delivery_service.create_zone(db_session, _circle(6000, "Couronne", fee=6, establishment_id=est))
    inner = await delivery_service.create_zone(db_session, _circle(2000, "Centre", fee=2, establishment_id=est))

    at_center = await delivery_service.check_address(db_session, *CENTER)
    assert at_center.id == inner.id

    dest = geometry.destination_point(CENTER[0], CENTER[1], 90, 4000)  # entre 2 km et 6 km
    in_ring = await delivery_service.check_address(db_session, dest[0], dest[1])
    assert in_ring.id == outer.id

    far = geometry.destination_point(CENTER[0], CENTER[1], 90, 9000)
    with pytest.raises(AppError) as exc:
        await delivery_service.check_address(db_session, far[0], far[1])
    assert exc.value.code == "DELIVERY_ZONE_UNREACHABLE"


async def test_zones_are_scoped_to_their_establishment(db_session):
    est_a, est_b = await _establishments(db_session)
    zone_a = await delivery_service.create_zone(db_session, _circle(3000, "Zone A", establishment_id=est_a))

    assert (await delivery_service.find_zone(db_session, *CENTER, establishment_id=est_a)).id == zone_a.id
    assert await delivery_service.find_zone(db_session, *CENTER, establishment_id=est_b) is None
    # Sans restriction, toutes les zones sont examinees.
    assert (await delivery_service.find_zone(db_session, *CENTER)).id == zone_a.id


async def test_legacy_zone_without_establishment_serves_every_establishment(db_session):
    est_a, est_b = await _establishments(db_session)
    from app.modules.delivery.models import DeliveryZone

    legacy = DeliveryZone(name="Historique", polygon=SQUARE, fee=3, min_order_amount=0, estimated_minutes=30)
    db_session.add(legacy)
    await db_session.flush()
    assert legacy.establishment_id is None
    for est in (est_a, est_b):
        assert (await delivery_service.find_zone(db_session, *CENTER, establishment_id=est)).id == legacy.id


async def test_inactive_zone_is_ignored_and_can_be_reactivated(db_session):
    est, _ = await _establishments(db_session)
    zone = await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est))

    await delivery_service.set_zone_active(db_session, zone.id, False)
    assert await delivery_service.find_zone(db_session, *CENTER) is None
    assert [z.id for z in await delivery_service.list_zones(db_session)] == []
    # Toujours visible cote administration (jamais supprimee).
    managed = await delivery_service.list_zones_for_management(db_session)
    assert zone.id in [z.id for z in managed]

    await delivery_service.set_zone_active(db_session, zone.id, True)
    assert (await delivery_service.find_zone(db_session, *CENTER)).id == zone.id


async def test_unreadable_legacy_polygon_does_not_break_other_zones(db_session):
    est, _ = await _establishments(db_session)
    from app.modules.delivery.models import DeliveryZone

    db_session.add(DeliveryZone(name="Cassee", polygon={"nope": 1}, fee=1, min_order_amount=0, estimated_minutes=10, establishment_id=est))
    good = await delivery_service.create_zone(db_session, _circle(3000, "Bonne", establishment_id=est))
    assert (await delivery_service.find_zone(db_session, *CENTER)).id == good.id


async def test_active_zone_capacity_is_limited_per_establishment(db_session, monkeypatch):
    est_a, est_b = await _establishments(db_session)
    monkeypatch.setattr(delivery_service, "MAX_ACTIVE_ZONES_PER_ESTABLISHMENT", 2)
    await delivery_service.create_zone(db_session, _circle(1000, "1", establishment_id=est_a))
    await delivery_service.create_zone(db_session, _circle(2000, "2", establishment_id=est_a))
    with pytest.raises(AppError) as exc:
        await delivery_service.create_zone(db_session, _circle(3000, "3", establishment_id=est_a))
    assert exc.value.code == "ZONE_LIMIT_REACHED" and exc.value.status_code == 409
    # Un autre etablissement n'est pas concerne, ni une zone creee inactive.
    await delivery_service.create_zone(db_session, _circle(3000, "autre", establishment_id=est_b))
    await delivery_service.create_zone(db_session, _circle(3000, "inactive", establishment_id=est_a, is_active=False))


async def test_update_zone_replaces_the_shape_and_keeps_rules_when_not_sent(db_session):
    est, _ = await _establishments(db_session)
    rule = DeliveryZoneRuleIn(label="Offerte des 25", kind="free", min_subtotal=25)
    zone = await delivery_service.create_zone(db_session, _circle(2000, establishment_id=est, rules=[rule]))
    assert len(zone.rules) == 1

    updated = await delivery_service.update_zone(db_session, zone.id, _circle(4000, "Renommee", fee=5))
    assert updated.name == "Renommee" and updated.fee == 5
    assert updated.shape_params["radius_m"] == 4000
    assert [r.label for r in updated.rules] == ["Offerte des 25"]  # rules=None : inchange

    cleared = await delivery_service.update_zone(db_session, zone.id, _circle(4000, "Renommee", fee=5, rules=[]))
    assert cleared.rules == []


async def test_update_unknown_zone_is_a_404(db_session):
    await _establishments(db_session)
    with pytest.raises(AppError) as exc:
        await delivery_service.update_zone(db_session, 999_999, _circle(1000))
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- regles


def test_rule_schema_validation():
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="x", kind="fee")  # fee requis
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="x", kind="free", days_of_week=[7])
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="x", kind="free", start_time="18:00")  # end_time manquant
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="x", kind="free", start_time="18:00", end_time="18:00")
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="x", kind="free", starts_on="2026-10-10", ends_on="2026-10-01")
    with pytest.raises(ValidationError):
        DeliveryZoneRuleIn(label="  ", kind="free")
    ok = DeliveryZoneRuleIn(label="x", kind="free", fee=9, days_of_week=[5, 1, 5])
    assert ok.fee is None and ok.days_of_week == [1, 5]  # fee ignore pour « free », jours tries et dedoublonnes


async def test_too_many_rules_are_refused(db_session):
    est, _ = await _establishments(db_session)
    zone = await delivery_service.create_zone(db_session, _circle(1000, establishment_id=est))
    rules = [DeliveryZoneRuleIn(label=f"r{i}", kind="free") for i in range(21)]
    with pytest.raises(AppError) as exc:
        await delivery_service._replace_rules(db_session, zone.id, rules)
    assert exc.value.code == "TOO_MANY_RULES"


async def test_quote_applies_zone_rules_in_the_establishment_timezone(db_session):
    (est,) = await _establishments(db_session, count=1)
    rules = [
        DeliveryZoneRuleIn(label="Offerte des 25 EUR", kind="free", min_subtotal=25),
        DeliveryZoneRuleIn(label="Tarif du soir", kind="fee", fee=5, start_time="19:00", end_time="23:00"),
    ]
    created = await delivery_service.create_zone(db_session, _circle(3000, fee=3, establishment_id=est, rules=rules))
    zone = await delivery_service.get_zone(db_session, created.id)

    # 18:30 UTC le 2 octobre 2026 = 20:30 a Paris (UTC+2) : tarif du soir.
    evening = datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc)
    quote = await delivery_service.quote_for_zone(db_session, zone, 20, now=evening)
    assert quote.pricing.fee == 5 and quote.pricing.applied_label == "Tarif du soir"
    assert quote.pricing.remaining_for_free == 5
    assert quote.min_order_met is True

    free = await delivery_service.quote_for_zone(db_session, zone, 30, now=evening)
    assert free.pricing.fee == 0 and free.pricing.free_delivery

    # 12:00 UTC = 14:00 Paris : hors fenetre du soir -> tarif de base.
    noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert (await delivery_service.quote_for_zone(db_session, zone, 20, now=noon)).pricing.fee == 3


async def test_quote_reports_minimum_order_and_unknown_subtotal(db_session):
    (est,) = await _establishments(db_session, count=1)
    created = await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est, min_order_amount=15))
    zone = await delivery_service.get_zone(db_session, created.id)
    assert (await delivery_service.quote_for_zone(db_session, zone, 10)).min_order_met is False
    assert (await delivery_service.quote_for_zone(db_session, zone, 15)).min_order_met is True
    assert (await delivery_service.quote_for_zone(db_session, zone, None)).min_order_met is None


# --------------------------------------------------------------------------- reglages / disponibilite


async def test_settings_default_to_enabled_and_conflicts_are_detected(db_session):
    row = await delivery_service.get_delivery_settings(db_session)
    assert row.internal_enabled is True and row.version == 1

    updated = await delivery_service.update_delivery_settings(
        db_session,
        internal_enabled=False,
        expected_version=1,
        user_id=1,
        user_email="admin@test.com",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    assert updated.internal_enabled is False and updated.version == 2
    audit = (await db_session.execute(sa.text("SELECT field_name, old_value, new_value, user_email FROM restaurant_delivery_settings_audits"))).all()
    assert [tuple(r) for r in audit] == [("internal_enabled", "true", "false", "admin@test.com")]

    with pytest.raises(AppError) as exc:
        await delivery_service.update_delivery_settings(
            db_session, internal_enabled=True, expected_version=1, user_id=1, user_email=None, ip_address=None, user_agent=None
        )
    assert exc.value.code == "DELIVERY_SETTINGS_CONFLICT" and exc.value.status_code == 409

    # Une mise a jour sans changement ne cree ni audit ni nouvelle version.
    same = await delivery_service.update_delivery_settings(
        db_session, internal_enabled=False, expected_version=2, user_id=1, user_email=None, ip_address=None, user_agent=None
    )
    assert same.version == 2


async def test_availability_reflects_switch_zones_and_locations(db_session):
    est_a, est_b = await _establishments(db_session)
    await delivery_service.set_establishment_location(db_session, est_a, 48.85, 2.35)
    await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est_a))

    info = await delivery_service.availability(db_session)
    by_id = {e["id"]: e for e in info["establishments"]}
    assert info["delivery_enabled"] is True
    assert by_id[est_a]["has_delivery_zones"] is True and by_id[est_a]["latitude"] == 48.85
    assert by_id[est_b]["has_delivery_zones"] is False and by_id[est_b]["latitude"] is None

    row = await delivery_service.get_delivery_settings(db_session)
    row.internal_enabled = False
    await db_session.flush()
    assert (await delivery_service.availability(db_session))["delivery_enabled"] is False


async def test_availability_is_false_without_any_zone(db_session):
    await _establishments(db_session)
    assert (await delivery_service.availability(db_session))["delivery_enabled"] is False


async def test_set_location_of_unknown_establishment_is_a_404(db_session):
    with pytest.raises(AppError) as exc:
        await delivery_service.set_establishment_location(db_session, 999_999, 1, 1)
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- creation de commande


async def test_order_fee_comes_from_the_innermost_zone_and_its_establishment(db_session):
    from app.modules.orders import service

    est_a, est_b = await _establishments(db_session)
    await delivery_service.create_zone(db_session, _circle(6000, "Couronne", fee=6, establishment_id=est_b))
    await delivery_service.create_zone(db_session, _circle(2000, "Centre", fee=2, establishment_id=est_a))
    product = await _product(db_session)

    order = await service.create_order(db_session, _delivery_body(product), user_id=1, idempotency_key="p1-inner")
    assert float(order.delivery_fee) == 2 and order.establishment_id == est_a  # etablissement deduit de la zone
    assert float(order.total) == 14


async def test_order_for_a_given_establishment_only_matches_its_own_zones(db_session):
    from app.modules.orders import service

    est_a, est_b = await _establishments(db_session)
    await delivery_service.create_zone(db_session, _circle(3000, "Zone A", establishment_id=est_a))
    product = await _product(db_session)

    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session, _delivery_body(product, establishment_id=est_b), user_id=1, idempotency_key="p1-scope-b"
        )
    assert exc.value.code == "DELIVERY_ZONE_UNREACHABLE"

    order = await service.create_order(
        db_session, _delivery_body(product, establishment_id=est_a), user_id=1, idempotency_key="p1-scope-a"
    )
    assert order.establishment_id == est_a


async def test_order_total_uses_free_delivery_rule_threshold(db_session):
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    rules = [DeliveryZoneRuleIn(label="Offerte des 24", kind="free", min_subtotal=24)]
    await delivery_service.create_zone(db_session, _circle(3000, fee=3, establishment_id=est, rules=rules))
    product = await _product(db_session)  # 12 EUR l'unite

    below = await service.create_order(db_session, _delivery_body(product, quantity=1), user_id=1, idempotency_key="p1-below")
    assert float(below.delivery_fee) == 3 and float(below.total) == 15

    at_threshold = await service.create_order(db_session, _delivery_body(product, quantity=2), user_id=1, idempotency_key="p1-at")
    assert float(at_threshold.delivery_fee) == 0 and float(at_threshold.total) == 24


async def test_order_minimum_is_enforced_from_the_matched_zone(db_session):
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est, min_order_amount=30))
    product = await _product(db_session)
    with pytest.raises(AppError) as exc:
        await service.create_order(db_session, _delivery_body(product), user_id=1, idempotency_key="p1-min")
    assert exc.value.code == "DELIVERY_MIN_ORDER_NOT_MET"


async def test_order_is_refused_when_delivery_is_switched_off(db_session):
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est))
    row = await delivery_service.get_delivery_settings(db_session)
    row.internal_enabled = False
    await db_session.flush()
    product = await _product(db_session)

    with pytest.raises(AppError) as exc:
        await service.create_order(db_session, _delivery_body(product), user_id=1, idempotency_key="p1-off")
    assert exc.value.code == "DELIVERY_DISABLED" and exc.value.status_code == 409

    # Le retrait n'est pas concerne.
    pickup = await service.create_order(
        db_session,
        _delivery_body(product, lat=None, lng=None).model_copy(update={"order_type": "pickup"}),
        user_id=1,
        idempotency_key="p1-off-pickup",
    )
    assert pickup.order_type == "pickup"


async def test_manual_order_zone_must_belong_to_the_order_establishment(db_session):
    from app.modules.orders import service

    est_a, est_b = await _establishments(db_session)
    zone_a = await delivery_service.create_zone(db_session, _circle(3000, establishment_id=est_a))
    product = await _product(db_session)
    body = _delivery_body(product, establishment_id=est_b, lat=None, lng=None).model_copy(
        update={"delivery_zone_id": zone_a.id}
    )
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session, body, user_id=None, idempotency_key="p1-manual-scope", source="manual", created_by_user_id=1
        )
    assert exc.value.code == "INVALID_DELIVERY_ZONE"


# --------------------------------------------------------------------------- promotion / fidelite


async def _free_delivery_promo(session, code="LIVRE0", discount_value=0, free_delivery=True):
    from app.modules.promotions.models import Promotion

    promo = Promotion(
        code=code,
        discount_type="fixed",
        discount_value=discount_value,
        free_delivery=free_delivery,
        min_order_amount=0,
        is_active=True,
    )
    session.add(promo)
    await session.flush()
    return promo


async def test_free_delivery_promo_waives_the_fee_without_discounting_products(db_session):
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    await delivery_service.create_zone(db_session, _circle(3000, fee=4, establishment_id=est))
    await _free_delivery_promo(db_session)
    product = await _product(db_session)

    order = await service.create_order(
        db_session, _delivery_body(product, promo_code="livre0"), user_id=1, idempotency_key="p1-promo"
    )
    assert float(order.delivery_fee) == 0 and float(order.discount_total) == 0 and float(order.total) == 12


async def test_free_delivery_promo_can_also_discount_products(db_session):
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    await delivery_service.create_zone(db_session, _circle(3000, fee=4, establishment_id=est))
    await _free_delivery_promo(db_session, code="COMBO", discount_value=2)
    product = await _product(db_session)

    order = await service.create_order(
        db_session, _delivery_body(product, promo_code="COMBO"), user_id=1, idempotency_key="p1-combo"
    )
    assert float(order.delivery_fee) == 0 and float(order.discount_total) == 2 and float(order.total) == 10


async def test_free_only_promo_is_refused_for_pickup_without_consuming_a_use(db_session):
    from app.modules.orders import service

    await _establishments(db_session, count=1)
    promo = await _free_delivery_promo(db_session)
    product = await _product(db_session)
    body = _delivery_body(product, lat=None, lng=None, promo_code="LIVRE0").model_copy(update={"order_type": "pickup"})

    with pytest.raises(AppError) as exc:
        await service.create_order(db_session, body, user_id=1, idempotency_key="p1-promo-pickup")
    assert exc.value.code == "PROMO_DELIVERY_ONLY"
    await db_session.refresh(promo)
    assert promo.current_uses == 0


def test_free_delivery_promo_schema_rules():
    from app.modules.promotions.schemas import PromotionCreate

    PromotionCreate(code="a", discount_type="fixed", discount_value=0, free_delivery=True)  # autorise
    with pytest.raises(ValidationError):
        PromotionCreate(code="a", discount_type="fixed", discount_value=0)  # remise nulle sans livraison offerte


async def test_loyalty_free_delivery_reward_mints_a_single_use_promo_code(db_session):
    from app.modules.loyalty.account.service import get_or_create_account
    from app.modules.loyalty.config.models import LoyaltyReward
    from app.modules.loyalty.config.service import redeem_reward
    from app.modules.promotions.models import Promotion

    reward = LoyaltyReward(name="Livraison offerte", reward_type="free_delivery", points_required=40, is_active=True)
    db_session.add(reward)
    account = await get_or_create_account(db_session, 777, commit=False)
    account.points = 100
    await db_session.flush()

    result = await redeem_reward(db_session, 777, reward.id)
    assert result.promo_code and result.promo_code.startswith("REWARD-")
    assert result.remaining_points == 60 and result.discount_euros is None

    promo = await db_session.scalar(sa.select(Promotion).where(Promotion.code == result.promo_code))
    assert (promo.free_delivery, float(promo.discount_value), promo.max_uses, promo.user_id, promo.is_public) == (
        True,
        0.0,
        1,
        777,
        False,
    )


async def test_counter_loyalty_free_delivery_reward_waives_the_fee_and_debits_points(db_session):
    from app.modules.loyalty.account.service import get_or_create_account
    from app.modules.loyalty.config.models import LoyaltyReward
    from app.modules.orders import service

    (est,) = await _establishments(db_session, count=1)
    await delivery_service.create_zone(db_session, _circle(3000, fee=4, establishment_id=est))
    reward = LoyaltyReward(name="Livraison offerte", reward_type="free_delivery", points_required=40, is_active=True)
    db_session.add(reward)
    account = await get_or_create_account(db_session, 888, commit=False)
    account.points = 100
    await db_session.flush()
    product = await _product(db_session)

    body = _delivery_body(product, loyalty_reward_id=reward.id) if False else None
    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    body = ManualOrderCreate(
        order_type="delivery",
        delivery_address="1 rue de Rivoli",
        delivery_lat=CENTER[0],
        delivery_lng=CENTER[1],
        customer={"full_name": "Client", "phone": "06 12 34 56 78"},
        loyalty_customer_id=888,
        loyalty_identification_method="phone",
        loyalty_reward_id=reward.id,
        loyalty_oral_confirmed=True,
        items=[OrderItemCreate(product_id=product.id, quantity=1)],
        payment={"method": "cash", "amount_received": 20},
    )
    order = await service.create_order(
        db_session,
        body,
        user_id=888,
        idempotency_key="p1-loyalty",
        source="manual",
        created_by_user_id=1,
        customer_phone="06 12 34 56 78",
        commit=False,
    )
    assert float(order.delivery_fee) == 0 and float(order.discount_total) == 0 and float(order.total) == 12
    await db_session.refresh(account)
    assert account.points == 60


async def test_loyalty_free_delivery_reward_is_refused_for_pickup(db_session):
    from app.modules.loyalty.account.service import get_or_create_account
    from app.modules.loyalty.config.models import LoyaltyReward
    from app.modules.orders import service
    from app.modules.orders.schemas import ManualOrderCreate, OrderItemCreate

    await _establishments(db_session, count=1)
    reward = LoyaltyReward(name="Livraison offerte", reward_type="free_delivery", points_required=40, is_active=True)
    db_session.add(reward)
    account = await get_or_create_account(db_session, 889, commit=False)
    account.points = 100
    await db_session.flush()
    product = await _product(db_session)

    body = ManualOrderCreate(
        order_type="pickup",
        loyalty_customer_id=889,
        loyalty_identification_method="phone",
        loyalty_reward_id=reward.id,
        loyalty_oral_confirmed=True,
        items=[OrderItemCreate(product_id=product.id, quantity=1)],
        payment={"method": "cash", "amount_received": 20},
    )
    with pytest.raises(AppError) as exc:
        await service.create_order(
            db_session, body, user_id=889, idempotency_key="p1-loyalty-pickup", source="manual", created_by_user_id=1, commit=False
        )
    assert exc.value.code == "REWARD_DELIVERY_REQUIRED"
    await db_session.refresh(account)
    assert account.points == 100
