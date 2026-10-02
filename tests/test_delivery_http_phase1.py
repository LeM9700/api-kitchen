"""Phase 1 livraison, niveau HTTP : droits, flux admin -> client, geocodage, limitation.

Ces requetes passent par l'application ASGI et commitent reellement : les zones creees sont
supprimees en fin de test. Les coordonnees (Belgrade) sont loin de celles des autres tests.
"""
import httpx
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from app.core.auth.security import create_access_token
from app.core.config import settings
from app.core.database import tenant_schema_name
from app.main import app
from app.modules.delivery import geocoding, geometry

BELGRADE = (44.7866, 20.4489)
FAR_AWAY = (45.5, 21.5)


def _token(boot, role):
    return create_access_token(
        {
            "sub": str(boot["staff_user_id"] if role == "staff" else boot["user_id"]),
            "email": f"{role}@test.com",
            "role": role,
            "tenant_id": boot["tenant_id"],
            "tenant_slug": boot["tenant_slug"],
            "permissions": None,
            "must_change_password": False,
        }
    )


@pytest.fixture
async def staff_client(bootstrap_default_tenant):
    headers = {"Authorization": "Bearer " + _token(bootstrap_default_tenant, "staff")}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
async def zone_cleanup(db_engine, bootstrap_default_tenant):
    created: list[int] = []
    yield created
    schema = tenant_schema_name(bootstrap_default_tenant["tenant_slug"])
    async with db_engine.begin() as conn:
        await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
        if created:
            await conn.execute(sa.text("DELETE FROM delivery_zones WHERE id = ANY(:ids)"), {"ids": created})
        await conn.execute(sa.text("DELETE FROM restaurant_delivery_settings_audits"))
        await conn.execute(sa.text("DELETE FROM restaurant_delivery_settings"))


def _circle_body(**overrides):
    body = {
        "name": "Zone HTTP",
        "fee": 3.5,
        "min_order_amount": 10,
        "estimated_minutes": 25,
        "shape": {"kind": "circle", "center_lat": BELGRADE[0], "center_lng": BELGRADE[1], "radius_m": 2500},
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- droits


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/v1/delivery/zones/manage", None),
        ("POST", "/api/v1/delivery/zones", {}),
        ("PUT", "/api/v1/delivery/zones/1", {}),
        ("PATCH", "/api/v1/delivery/zones/1", {"is_active": False}),
        ("DELETE", "/api/v1/delivery/zones/1", None),
        ("PUT", "/api/v1/delivery/zones/1/rules", []),
        ("POST", "/api/v1/delivery/zones/preview", {}),
        ("GET", "/api/v1/delivery/settings", None),
        ("PUT", "/api/v1/delivery/settings", {}),
        ("PUT", "/api/v1/delivery/establishments/1/location", {}),
        ("POST", "/api/v1/delivery/check", {"lat": 1, "lng": 1}),
        ("GET", "/api/v1/delivery/geocode?q=rivoli", None),
        ("GET", "/api/v1/delivery/reverse-geocode?lat=1&lng=1", None),
    ],
)
async def test_protected_endpoints_require_authentication(client, method, path, payload):
    response = await client.request(method, path, json=payload)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/api/v1/delivery/zones", _circle_body()),
        ("PUT", "/api/v1/delivery/zones/1", _circle_body()),
        ("PATCH", "/api/v1/delivery/zones/1", {"is_active": False}),
        ("DELETE", "/api/v1/delivery/zones/1", None),
        ("PUT", "/api/v1/delivery/zones/1/rules", []),
        ("POST", "/api/v1/delivery/zones/preview", {"shape": _circle_body()["shape"]}),
        ("PUT", "/api/v1/delivery/settings", {"internal_enabled": False, "expected_version": 1}),
        ("PUT", "/api/v1/delivery/establishments/1/location", {"latitude": 1, "longitude": 1}),
    ],
)
async def test_staff_cannot_modify_zones_or_settings(staff_client, method, path, payload):
    response = await staff_client.request(method, path, json=payload)
    assert response.status_code == 403


async def test_staff_can_read_zones_and_settings(staff_client, zone_cleanup):
    assert (await staff_client.get("/api/v1/delivery/zones/manage")).status_code == 200
    settings_response = await staff_client.get("/api/v1/delivery/settings")
    assert settings_response.status_code == 200
    assert settings_response.json()["internal_enabled"] is True


# --------------------------------------------------------------------------- apercu / validation


async def test_preview_returns_a_polygon_without_saving(authed_client):
    response = await authed_client.post("/api/v1/delivery/zones/preview", json={"shape": _circle_body()["shape"]})
    assert response.status_code == 200
    data = response.json()
    assert data["shape_kind"] == "circle" and data["points"] == 64
    assert data["area_km2"] == pytest.approx(3.14159 * 2.5**2, rel=0.03)
    assert data["polygon"]["type"] == "Polygon"


@pytest.mark.parametrize(
    "shape,code",
    [
        ({"kind": "circle", "center_lat": 44.78, "center_lng": 20.44, "radius_m": 10}, "CIRCLE_RADIUS_OUT_OF_RANGE"),
        (
            {"kind": "polygon", "polygon": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}},
            "POLYGON_SELF_INTERSECTING",
        ),
        ({"kind": "polygon", "polygon": {"type": "MultiPolygon", "coordinates": []}}, "GEOJSON_UNSUPPORTED"),
    ],
)
async def test_invalid_shapes_return_a_stable_422_code(authed_client, shape, code):
    response = await authed_client.post("/api/v1/delivery/zones/preview", json={"shape": shape})
    assert response.status_code == 422
    assert response.json()["code"] == code


async def test_malformed_shape_payload_is_rejected_by_validation(authed_client):
    response = await authed_client.post(
        "/api/v1/delivery/zones/preview", json={"shape": {"kind": "circle", "center_lat": 200, "center_lng": 0, "radius_m": 500}}
    )
    assert response.status_code == 422
    response = await authed_client.post("/api/v1/delivery/zones/preview", json={"shape": {"kind": "star"}})
    assert response.status_code == 422


async def test_isochrone_preview_without_token_is_a_503(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", "")
    response = await authed_client.post(
        "/api/v1/delivery/zones/preview",
        json={"shape": {"kind": "isochrone", "center_lat": BELGRADE[0], "center_lng": BELGRADE[1], "minutes": 10}},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "GEOCODING_NOT_CONFIGURED"


# --------------------------------------------------------------------------- flux complet


async def test_admin_creates_a_zone_then_the_customer_checks_an_address(authed_client, bootstrap_default_tenant, zone_cleanup):
    slug = bootstrap_default_tenant["tenant_slug"]
    created = await authed_client.post(
        "/api/v1/delivery/zones",
        json=_circle_body(
            rules=[
                {"label": "Offerte des 25", "kind": "free", "min_subtotal": 25},
            ]
        ),
    )
    assert created.status_code == 201, created.text
    zone = created.json()
    zone_cleanup.append(zone["id"])
    assert zone["shape_kind"] == "circle" and zone["polygon"]["type"] == "Polygon"
    assert [r["label"] for r in zone["rules"]] == ["Offerte des 25"]

    # Liste publique : pas de contour (on ne cartographie pas la couverture du restaurant).
    public = await authed_client.get("/api/v1/delivery/zones", headers={"X-Tenant-Slug": slug})
    assert public.status_code == 200
    mine = next(z for z in public.json() if z["id"] == zone["id"])
    assert "polygon" not in mine and mine["fee"] == 3.5

    # Liste d'administration : avec contour et regles.
    managed = (await authed_client.get("/api/v1/delivery/zones/manage")).json()
    assert next(z for z in managed if z["id"] == zone["id"])["polygon"]["type"] == "Polygon"

    inside = await authed_client.post(
        "/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1], "subtotal": 20}
    )
    assert inside.status_code == 200, inside.text
    data = inside.json()
    assert (data["zone_id"], data["fee"], data["base_fee"], data["free_delivery"]) == (zone["id"], 3.5, 3.5, False)
    assert data["min_order_amount"] == 10 and data["min_order_met"] is True
    assert data["remaining_for_free"] == 5 and data["estimated_minutes"] == 25
    assert data["establishment_id"] == zone["establishment_id"]

    free = (
        await authed_client.post("/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1], "subtotal": 30})
    ).json()
    assert (free["fee"], free["free_delivery"], free["applied"], free["applied_label"]) == (0, True, "rule", "Offerte des 25")

    below_minimum = (
        await authed_client.post("/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1], "subtotal": 5})
    ).json()
    assert below_minimum["min_order_met"] is False

    without_subtotal = (
        await authed_client.post("/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1]})
    ).json()
    assert without_subtotal["min_order_met"] is None

    outside = await authed_client.post("/api/v1/delivery/check", json={"lat": FAR_AWAY[0], "lng": FAR_AWAY[1]})
    assert outside.status_code == 422 and outside.json()["code"] == "DELIVERY_ZONE_UNREACHABLE"

    # Desactivation : la zone disparait de la liste publique et de la verification, mais pas de l'admin.
    deactivated = await authed_client.delete(f"/api/v1/delivery/zones/{zone['id']}")
    assert deactivated.status_code == 200 and deactivated.json()["is_active"] is False
    gone = await authed_client.post("/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1]})
    assert gone.status_code == 422
    public_after = (await authed_client.get("/api/v1/delivery/zones", headers={"X-Tenant-Slug": slug})).json()
    assert zone["id"] not in [z["id"] for z in public_after]
    assert zone["id"] in [z["id"] for z in (await authed_client.get("/api/v1/delivery/zones/manage")).json()]

    reactivated = await authed_client.patch(f"/api/v1/delivery/zones/{zone['id']}", json={"is_active": True})
    assert reactivated.json()["is_active"] is True


async def test_rules_endpoint_replaces_and_validates(authed_client, zone_cleanup):
    created = (await authed_client.post("/api/v1/delivery/zones", json=_circle_body())).json()
    zone_cleanup.append(created["id"])

    ok = await authed_client.put(
        f"/api/v1/delivery/zones/{created['id']}/rules",
        json=[
            {"label": "Vendredi soir", "kind": "fee", "fee": 6, "days_of_week": [4], "start_time": "19:00:00", "end_time": "23:00:00"},
            {"label": "Noel", "kind": "free", "starts_on": "2026-12-24", "ends_on": "2026-12-25"},
        ],
    )
    assert ok.status_code == 200 and len(ok.json()["rules"]) == 2
    assert ok.json()["rules"][0]["start_time"] == "19:00:00"

    bad = await authed_client.put(
        f"/api/v1/delivery/zones/{created['id']}/rules", json=[{"label": "Sans tarif", "kind": "fee"}]
    )
    assert bad.status_code == 422

    cleared = await authed_client.put(f"/api/v1/delivery/zones/{created['id']}/rules", json=[])
    assert cleared.json()["rules"] == []


async def test_unknown_zone_returns_404_everywhere(authed_client):
    assert (await authed_client.put("/api/v1/delivery/zones/999999", json=_circle_body())).status_code == 404
    assert (await authed_client.patch("/api/v1/delivery/zones/999999", json={"is_active": False})).status_code == 404
    assert (await authed_client.delete("/api/v1/delivery/zones/999999")).status_code == 404
    assert (await authed_client.put("/api/v1/delivery/zones/999999/rules", json=[])).status_code == 404


async def test_check_validates_input(authed_client):
    assert (await authed_client.post("/api/v1/delivery/check", json={"lat": 95, "lng": 0})).status_code == 422
    assert (await authed_client.post("/api/v1/delivery/check", json={"lat": 1, "lng": 200})).status_code == 422
    assert (await authed_client.post("/api/v1/delivery/check", json={"lat": 1, "lng": 1, "subtotal": -1})).status_code == 422


# --------------------------------------------------------------------------- reglages


async def test_switching_delivery_off_blocks_check_and_updates_availability(authed_client, bootstrap_default_tenant, zone_cleanup):
    slug = bootstrap_default_tenant["tenant_slug"]
    zone = (await authed_client.post("/api/v1/delivery/zones", json=_circle_body())).json()
    zone_cleanup.append(zone["id"])

    current = (await authed_client.get("/api/v1/delivery/settings")).json()
    assert current["internal_enabled"] is True and current["version"] == 1

    off = await authed_client.put(
        "/api/v1/delivery/settings", json={"internal_enabled": False, "expected_version": current["version"]}
    )
    assert off.status_code == 200 and off.json()["version"] == 2

    stale = await authed_client.put(
        "/api/v1/delivery/settings", json={"internal_enabled": True, "expected_version": 1}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "DELIVERY_SETTINGS_CONFLICT"

    blocked = await authed_client.post("/api/v1/delivery/check", json={"lat": BELGRADE[0], "lng": BELGRADE[1]})
    assert blocked.status_code == 409 and blocked.json()["code"] == "DELIVERY_DISABLED"

    availability = (await authed_client.get("/api/v1/delivery/availability", headers={"X-Tenant-Slug": slug})).json()
    assert availability["delivery_enabled"] is False


async def test_availability_is_public_and_well_formed(client, bootstrap_default_tenant):
    response = await client.get("/api/v1/delivery/availability", headers={"X-Tenant-Slug": bootstrap_default_tenant["tenant_slug"]})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"delivery_enabled", "establishments"}
    for establishment in data["establishments"]:
        assert set(establishment) == {"id", "name", "latitude", "longitude", "has_delivery_zones"}


async def test_establishment_location_can_be_set_by_admin(authed_client, db_engine, bootstrap_default_tenant, zone_cleanup):
    schema = tenant_schema_name(bootstrap_default_tenant["tenant_slug"])
    async with db_engine.begin() as conn:
        await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
        establishment_id = await conn.scalar(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
    if establishment_id is None:
        pytest.skip("aucun etablissement dans le tenant de test")

    ok = await authed_client.put(
        f"/api/v1/delivery/establishments/{establishment_id}/location", json={"latitude": 44.8, "longitude": 20.46}
    )
    assert ok.status_code == 200 and ok.json()["latitude"] == 44.8

    assert (
        await authed_client.put("/api/v1/delivery/establishments/999999/location", json={"latitude": 1, "longitude": 1})
    ).status_code == 404
    assert (
        await authed_client.put(f"/api/v1/delivery/establishments/{establishment_id}/location", json={"latitude": 91, "longitude": 1})
    ).status_code == 422

    async with db_engine.begin() as conn:  # restitue l'etat d'origine
        await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
        await conn.execute(sa.text("UPDATE establishments SET latitude = NULL, longitude = NULL WHERE id = :id"), {"id": establishment_id})


# --------------------------------------------------------------------------- geocodage


async def test_geocode_without_token_is_a_503(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", "")
    response = await authed_client.get("/api/v1/delivery/geocode", params={"q": "rue de rivoli"})
    assert response.status_code == 503 and response.json()["code"] == "GEOCODING_NOT_CONFIGURED"


async def test_geocode_and_reverse_geocode_return_the_proxy_results(authed_client, monkeypatch):
    payload = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [20.4489, 44.7866]},
                "properties": {"name": "Knez Mihailova 1", "full_address": "Knez Mihailova 1, Beograd, Srbija",
                               "coordinates": {"latitude": 44.7866, "longitude": 20.4489},
                               "context": {"place": {"name": "Beograd"}, "country": {"country_code": "RS"}}},
            }
        ]
    }
    monkeypatch.setattr(settings, "mapbox_access_token", "tok")
    geocoding.clear_cache()
    monkeypatch.setattr(
        geocoding, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    )

    found = await authed_client.get(
        "/api/v1/delivery/geocode", params={"q": "knez mihailova", "lat": 44.78, "lng": 20.44, "language": "sr"}
    )
    assert found.status_code == 200
    assert found.json()[0]["label"] == "Knez Mihailova 1, Beograd, Srbija" and found.json()[0]["country_code"] == "rs"

    reverse = await authed_client.get("/api/v1/delivery/reverse-geocode", params={"lat": 44.7866, "lng": 20.4489})
    assert reverse.status_code == 200 and reverse.json()["city"] == "Beograd"

    geocoding.clear_cache()
    monkeypatch.setattr(
        geocoding, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"features": []})))
    )
    assert (await authed_client.get("/api/v1/delivery/reverse-geocode", params={"lat": 1, "lng": 1})).json() is None


@pytest.mark.parametrize("params", [{"q": "ab"}, {"q": ""}, {"q": "rue", "limit": 99}, {"q": "rue", "lat": 120, "lng": 0}])
async def test_geocode_validates_its_parameters(authed_client, monkeypatch, params):
    monkeypatch.setattr(settings, "mapbox_access_token", "tok")
    response = await authed_client.get("/api/v1/delivery/geocode", params=params)
    assert response.status_code == 422


async def test_geocode_is_rate_limited_per_user(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "mapbox_access_token", "")  # 503 rapides : seul le compteur nous interesse
    statuses = [
        (await authed_client.get("/api/v1/delivery/geocode", params={"q": "rue de rivoli"})).status_code for _ in range(62)
    ]
    assert statuses[:60] == [503] * 60
    assert 429 in statuses[60:]


async def test_establishments_list_exposes_the_location_used_to_center_maps(
    staff_client, db_engine, bootstrap_default_tenant
):
    schema = tenant_schema_name(bootstrap_default_tenant["tenant_slug"])
    async with db_engine.begin() as conn:
        await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
        establishment_id = await conn.scalar(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
        if establishment_id is None:
            pytest.skip("aucun etablissement dans le tenant de test")
        await conn.execute(
            sa.text("UPDATE establishments SET latitude = 44.8, longitude = 20.46 WHERE id = :id"),
            {"id": establishment_id},
        )
    try:
        response = await staff_client.get("/api/v1/tenant/establishments")
        if response.status_code == 404:  # prefixe different selon le montage du routeur
            response = await staff_client.get("/api/v1/tenant/establishments")
        assert response.status_code == 200, response.text
        item = next(e for e in response.json() if e["id"] == establishment_id)
        assert (item["latitude"], item["longitude"]) == (44.8, 20.46)
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(sa.text(f'SET search_path TO "{schema}"'))
            await conn.execute(
                sa.text("UPDATE establishments SET latitude = NULL, longitude = NULL WHERE id = :id"),
                {"id": establishment_id},
            )
