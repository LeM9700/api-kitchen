"""Tests des routes HTTP du module `delivery` (Plan 01, Tache 3 -- durcissement).

Ces tests isolent le router en mockant `app.modules.delivery.router.service.*`
(meme convention que `tests/test_customer.py`) : le but est de verifier le
mapping statut HTTP / code d'erreur / forme de reponse du router, pas le
comportement reel des requetes DB (couvert separement, sans HTTP, dans
`tests/test_delivery_zones.py`).

Couverture (Tache 3) :
    GET    /delivery/zones      -- header absent -> 400 TENANT_REQUIRED,
                                    slug inconnu -> 404 TENANT_NOT_FOUND,
                                    slug connu -> 200.
    POST   /delivery/zones      -- delegue a `service.create_zone`.
    PUT    /delivery/zones/{id} -- zone absente -> 404 DELIVERY_ZONE_NOT_FOUND
                                    (jamais 500), succes -> 200.
    DELETE /delivery/zones/{id} -- 204, admin-only, zone absente -> 404.
    POST   /delivery/check      -- forme de reponse `AddressCheckOut` inchangee,
                                    bornes lat/lng.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.http.deps import get_current_user
from app.main import app
from app.modules.delivery.common.errors import DeliveryZoneNotFoundError, TenantNotFoundError
from app.modules.delivery.schemas import AddressCheckRequest, DeliveryZoneCreate

ZONES_URL = "/api/v1/delivery/zones"
CHECK_URL = "/api/v1/delivery/check"


def _square_polygon() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [2.30, 48.85],
                [2.35, 48.85],
                [2.35, 48.90],
                [2.30, 48.90],
                [2.30, 48.85],
            ]
        ],
    }


VALID_ZONE_BODY = {
    "name": "Centre-ville",
    "polygon": _square_polygon(),
    "fee": 2.5,
    "min_order_amount": 10,
    "estimated_minutes": 30,
}


def _admin_user_dict() -> dict:
    return {
        "id": "7",
        "tenant_id": 1,
        "tenant_slug": "test-tenant",
        "role": "admin",
        "email": "admin@example.com",
        "must_change_password": False,
    }


def _customer_user_dict() -> dict:
    return {
        "id": "42",
        "tenant_id": 1,
        "tenant_slug": "test-tenant",
        "role": "customer",
        "email": "customer@example.com",
        "must_change_password": False,
    }


def _override_current_user(user_dict: dict):
    async def _dep():
        return user_dict
    return _dep


# ---------------------------------------------------------------------------
# POST /api/v1/delivery/check
# ---------------------------------------------------------------------------


async def test_delivery_check_requires_auth(client):
    response = await client.post(CHECK_URL, json={"lat": 48.8566, "lng": 2.3522})
    assert response.status_code == 401


async def test_check_address_response_shape_is_unchanged(client, monkeypatch):
    """Le corps JSON de /check doit garder les memes cles/types qu'avant
    l'introduction de `AddressCheckOut` (compatibilite app-client)."""
    fake_zone = SimpleNamespace(id=3, name="Centre-ville", fee=2.5, estimated_minutes=25)

    async def fake_check_address(session, lat, lng):
        return fake_zone

    monkeypatch.setattr("app.modules.delivery.router.service.check_address", fake_check_address)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.post(CHECK_URL, json={"lat": 48.8566, "lng": 2.3522})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"zone_id", "name", "fee", "estimated_minutes"}
    assert body == {"zone_id": 3, "name": "Centre-ville", "fee": 2.5, "estimated_minutes": 25}
    assert isinstance(body["zone_id"], int)
    assert isinstance(body["name"], str)
    assert isinstance(body["fee"], float)
    assert isinstance(body["estimated_minutes"], int)


@pytest.mark.parametrize("lat,lng", [(95, 2.35), (-95, 2.35), (48.85, 185), (48.85, -185)])
def test_address_check_request_rejects_out_of_range_coordinates(lat, lng):
    with pytest.raises(ValidationError):
        AddressCheckRequest(lat=lat, lng=lng)


@pytest.mark.parametrize("lat,lng", [(90, 180), (-90, -180), (0, 0)])
def test_address_check_request_accepts_boundary_coordinates(lat, lng):
    body = AddressCheckRequest(lat=lat, lng=lng)
    assert body.lat == lat
    assert body.lng == lng


# ---------------------------------------------------------------------------
# GET /api/v1/delivery/zones -- tenant slug helper
# ---------------------------------------------------------------------------


async def test_list_zones_missing_header_returns_tenant_required(client):
    response = await client.get(ZONES_URL)
    assert response.status_code == 400
    assert response.json()["code"] == "TENANT_REQUIRED"


async def test_list_zones_unknown_slug_returns_tenant_not_found(client, monkeypatch):
    async def fake_resolve(tenant_slug):
        raise TenantNotFoundError(tenant_slug)

    monkeypatch.setattr("app.modules.delivery.router.service.resolve_known_tenant_slug", fake_resolve)

    response = await client.get(ZONES_URL, headers={"X-Tenant-Slug": "does-not-exist"})
    assert response.status_code == 404
    assert response.json()["code"] == "TENANT_NOT_FOUND"


async def test_list_zones_known_slug_returns_200(client, monkeypatch):
    async def fake_resolve(tenant_slug):
        return tenant_slug

    async def fake_list_zones(session):
        return []

    monkeypatch.setattr("app.modules.delivery.router.service.resolve_known_tenant_slug", fake_resolve)
    monkeypatch.setattr("app.modules.delivery.router.service.list_zones", fake_list_zones)

    response = await client.get(ZONES_URL, headers={"X-Tenant-Slug": "test-tenant"})
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# POST /api/v1/delivery/zones
# ---------------------------------------------------------------------------


async def test_create_zone_requires_admin(client):
    response = await client.post(ZONES_URL, json=VALID_ZONE_BODY)
    assert response.status_code == 401


async def test_create_zone_delegates_to_service(client, monkeypatch):
    fake_zone = SimpleNamespace(
        id=1, name="Centre-ville", fee=2.5, min_order_amount=10.0, estimated_minutes=30, is_active=True
    )

    async def fake_create_zone(session, body):
        return fake_zone

    monkeypatch.setattr("app.modules.delivery.router.service.create_zone", fake_create_zone)

    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.post(ZONES_URL, json=VALID_ZONE_BODY)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 201
    assert response.json()["id"] == 1


# ---------------------------------------------------------------------------
# PUT /api/v1/delivery/zones/{id}
# ---------------------------------------------------------------------------


async def test_update_zone_not_found_returns_404_not_500(client, monkeypatch):
    async def fake_update_zone(session, zone_id, body):
        raise DeliveryZoneNotFoundError(zone_id)

    monkeypatch.setattr("app.modules.delivery.router.service.update_zone", fake_update_zone)

    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.put(f"{ZONES_URL}/999999", json=VALID_ZONE_BODY)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert response.json()["code"] == "DELIVERY_ZONE_NOT_FOUND"


async def test_update_zone_success_returns_200(client, monkeypatch):
    fake_zone = SimpleNamespace(
        id=5, name="Centre-ville", fee=2.5, min_order_amount=10.0, estimated_minutes=30, is_active=True
    )

    async def fake_update_zone(session, zone_id, body):
        return fake_zone

    monkeypatch.setattr("app.modules.delivery.router.service.update_zone", fake_update_zone)

    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.put(f"{ZONES_URL}/5", json=VALID_ZONE_BODY)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["id"] == 5


# ---------------------------------------------------------------------------
# DELETE /api/v1/delivery/zones/{id}
# ---------------------------------------------------------------------------


async def test_delete_zone_requires_admin(client):
    response = await client.delete(f"{ZONES_URL}/1")
    assert response.status_code == 401


async def test_delete_zone_wrong_role_forbidden(client):
    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.delete(f"{ZONES_URL}/1")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 403


async def test_delete_zone_success_returns_204(client, monkeypatch):
    async def fake_delete_zone(session, zone_id):
        return None

    monkeypatch.setattr("app.modules.delivery.router.service.delete_zone", fake_delete_zone)

    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.delete(f"{ZONES_URL}/1")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 204
    assert response.content == b""


async def test_delete_zone_not_found_returns_404_not_500(client, monkeypatch):
    async def fake_delete_zone(session, zone_id):
        raise DeliveryZoneNotFoundError(zone_id)

    monkeypatch.setattr("app.modules.delivery.router.service.delete_zone", fake_delete_zone)

    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.delete(f"{ZONES_URL}/999999")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert response.json()["code"] == "DELIVERY_ZONE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Bornes Pydantic -- DeliveryZoneCreate
# ---------------------------------------------------------------------------


def test_delivery_zone_create_rejects_negative_fee():
    with pytest.raises(ValidationError):
        DeliveryZoneCreate(name="Zone", polygon=_square_polygon(), fee=-1)


def test_delivery_zone_create_rejects_negative_min_order_amount():
    with pytest.raises(ValidationError):
        DeliveryZoneCreate(name="Zone", polygon=_square_polygon(), fee=2.5, min_order_amount=-5)


@pytest.mark.parametrize("minutes", [0, -1, -30])
def test_delivery_zone_create_rejects_non_positive_estimated_minutes(minutes):
    with pytest.raises(ValidationError):
        DeliveryZoneCreate(name="Zone", polygon=_square_polygon(), fee=2.5, estimated_minutes=minutes)


def test_delivery_zone_create_accepts_zero_fee_and_min_order_amount():
    zone = DeliveryZoneCreate(name="Zone", polygon=_square_polygon(), fee=0, min_order_amount=0)
    assert zone.fee == 0
    assert zone.min_order_amount == 0
