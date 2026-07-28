"""Tests for the customer module endpoints.

Coverage:
    POST /api/v1/customer/register — success, TENANT_NOT_FOUND, EMAIL_ALREADY_EXISTS,
        weak password (422), missing X-Tenant-Slug header (422)
    GET  /api/v1/customer/me      — success, wrong role (403)
    PATCH /api/v1/customer/me     — full_name update, phone update
    DELETE /api/v1/customer/me   — success (204), wrong password (401)
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock


from app.core.http.deps import get_current_user
from app.core.http.errors import AppError
from app.main import app
from app.modules.customer.schemas import CustomerDataExportOut, CustomerOrderExportOut, CustomerOut

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

REGISTER_URL = "/api/v1/customer/register"
ME_URL = "/api/v1/customer/me"

VALID_REGISTER_BODY = {
    "email": "newcustomer@example.com",
    "password": "Secure1!",
    "full_name": "New Customer",
    "phone": "+33612345678",
}

_CUSTOMER_OUT = CustomerOut(
    id=42,
    email="customer@example.com",
    full_name="Test Customer",
    phone="+33600000000",
    role="customer",
    email_verified=False,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
)


def _customer_user_dict() -> dict:
    return {
        "id": "42",
        "tenant_id": 1,
        "tenant_slug": "test-tenant",
        "role": "customer",
        "email": "customer@example.com",
        "must_change_password": False,
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


def _override_current_user(user_dict: dict):
    """Return a FastAPI dependency override that yields a fixed user dict."""
    async def _dep():
        return user_dict
    return _dep


# ---------------------------------------------------------------------------
# POST /api/v1/customer/register
# ---------------------------------------------------------------------------


async def test_register_success(client, monkeypatch):
    """201 with tokens when service.register succeeds."""
    mock_user = MagicMock()

    async def fake_register(tenant_slug, body, arq_pool=None):
        return (mock_user, "access_tok", "refresh_tok", 99)

    monkeypatch.setattr("app.modules.customer.router.service.register", fake_register)

    response = await client.post(
        REGISTER_URL,
        json=VALID_REGISTER_BODY,
        headers={"x-tenant-slug": "test-tenant"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["access_token"] == "access_tok"
    assert data["refresh_token"] == "refresh_tok"
    assert data["session_id"] == 99


async def test_register_tenant_not_found(client, monkeypatch):
    """404 when service raises TENANT_NOT_FOUND."""
    async def fake_register(tenant_slug, body, arq_pool=None):
        raise AppError("TENANT_NOT_FOUND", "Tenant not found", 404, "tenant_slug")

    monkeypatch.setattr("app.modules.customer.router.service.register", fake_register)

    response = await client.post(
        REGISTER_URL,
        json=VALID_REGISTER_BODY,
        headers={"x-tenant-slug": "unknown-tenant"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "TENANT_NOT_FOUND"


async def test_register_email_already_exists(client, monkeypatch):
    """409 when service raises EMAIL_ALREADY_EXISTS."""
    async def fake_register(tenant_slug, body, arq_pool=None):
        raise AppError("EMAIL_ALREADY_EXISTS", "Email already registered", 409, "email")

    monkeypatch.setattr("app.modules.customer.router.service.register", fake_register)

    response = await client.post(
        REGISTER_URL,
        json=VALID_REGISTER_BODY,
        headers={"x-tenant-slug": "test-tenant"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_ALREADY_EXISTS"


async def test_register_weak_password(client):
    """422 when password lacks uppercase (fails schema validation before hitting service)."""
    body = {**VALID_REGISTER_BODY, "password": "nouppercase1!"}

    response = await client.post(
        REGISTER_URL,
        json=body,
        headers={"x-tenant-slug": "test-tenant"},
    )

    assert response.status_code == 422


async def test_register_missing_tenant_header(client):
    """400 when X-Tenant-Slug header is absent (router raises MISSING_TENANT_SLUG)."""
    response = await client.post(REGISTER_URL, json=VALID_REGISTER_BODY)
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_TENANT_SLUG"


# ---------------------------------------------------------------------------
# GET /api/v1/customer/me
# ---------------------------------------------------------------------------


async def test_get_me_success(client, monkeypatch):
    """200 with CustomerOut when customer calls GET /me."""
    async def fake_get_profile(user_id, tenant_slug):
        return _CUSTOMER_OUT

    monkeypatch.setattr("app.modules.customer.router.service.get_profile", fake_get_profile)

    # Override get_current_user — require_role("customer") depends on it.
    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.get(ME_URL)
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "customer@example.com"
        assert data["role"] == "customer"
        assert "email_verified" in data
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_get_me_wrong_role_forbidden(client):
    """403 when a non-customer (admin) calls GET /me."""
    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.get(ME_URL)
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# PATCH /api/v1/customer/me
# ---------------------------------------------------------------------------


async def test_patch_me_full_name(client, monkeypatch):
    """200 when updating full_name."""
    updated = _CUSTOMER_OUT.model_copy(update={"full_name": "New Name"})

    async def fake_update_profile(user_id, tenant_slug, body):
        return updated

    monkeypatch.setattr("app.modules.customer.router.service.update_profile", fake_update_profile)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.patch(ME_URL, json={"full_name": "New Name"})
        assert response.status_code == 200
        assert response.json()["full_name"] == "New Name"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_patch_me_phone(client, monkeypatch):
    """200 when updating phone only."""
    updated = _CUSTOMER_OUT.model_copy(update={"phone": "+33612345678"})

    async def fake_update_profile(user_id, tenant_slug, body):
        return updated

    monkeypatch.setattr("app.modules.customer.router.service.update_profile", fake_update_profile)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.patch(ME_URL, json={"phone": "+33612345678"})
        assert response.status_code == 200
        assert response.json()["phone"] == "+33612345678"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_update_me_invalid_phone(client):
    """422 when PATCH /me receives an invalid phone number."""
    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.patch(
            ME_URL,
            json={"phone": "not-a-phone-number-!!!"},
        )
        assert response.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_get_me_unauthenticated(client):
    """401 when GET /me is called without a token (no dependency override)."""
    # No dependency override — let the real auth dependency run and reject the request.
    response = await client.get(ME_URL)
    assert response.status_code == 401


async def test_update_me_both_fields(client, monkeypatch):
    """200 when PATCH /me updates both full_name and phone."""
    from app.modules.customer import service as customer_service

    mock_out = CustomerOut(
        id=42,
        email="c@example.com",
        full_name="Updated Name",
        phone="+33612345678",
        role="customer",
        email_verified=False,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    async def fake_update(user_id, tenant_slug, body):
        return mock_out

    monkeypatch.setattr(customer_service, "update_profile", fake_update)
    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.patch(
            ME_URL,
            json={"full_name": "Updated Name", "phone": "+33612345678"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["full_name"] == "Updated Name"
        assert data["phone"] == "+33612345678"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# DELETE /api/v1/customer/me
# ---------------------------------------------------------------------------


async def test_delete_me_success(client, monkeypatch):
    """204 when service.delete_account completes without error."""
    async def fake_delete_account(user_id, tenant_slug, password, redis=None):
        return None

    monkeypatch.setattr("app.modules.customer.router.service.delete_account", fake_delete_account)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.request(
            "DELETE",
            ME_URL,
            content=json.dumps({"password": "Pass1!ok"}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 204
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_delete_me_wrong_password(client, monkeypatch):
    """401 when service raises INVALID_CREDENTIALS."""
    async def fake_delete_account(user_id, tenant_slug, password, redis=None):
        raise AppError("INVALID_CREDENTIALS", "Invalid password", 401)

    monkeypatch.setattr("app.modules.customer.router.service.delete_account", fake_delete_account)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.request(
            "DELETE",
            ME_URL,
            content=json.dumps({"password": "wrongpassword"}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "INVALID_CREDENTIALS"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# GET /api/v1/customer/me/export
# ---------------------------------------------------------------------------


async def test_export_me_success(client, monkeypatch):
    """200 with profile + orders when customer calls GET /me/export."""
    export_out = CustomerDataExportOut(
        profile=_CUSTOMER_OUT,
        orders=[
            CustomerOrderExportOut(
                id=1, status="delivered", total=15.5, created_at=datetime(2024, 1, 2, tzinfo=timezone.utc)
            )
        ],
        orders_truncated=False,
        exported_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
    )

    async def fake_export_my_data(user_id, tenant_slug):
        return export_out

    monkeypatch.setattr("app.modules.customer.router.service.export_my_data", fake_export_my_data)

    app.dependency_overrides[get_current_user] = _override_current_user(_customer_user_dict())
    try:
        response = await client.get("/api/v1/customer/me/export")
        assert response.status_code == 200
        data = response.json()
        assert data["profile"]["email"] == "customer@example.com"
        assert len(data["orders"]) == 1
        assert data["orders"][0]["id"] == 1
        assert data["orders_truncated"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_export_me_wrong_role_forbidden(client):
    """403 when a non-customer (admin) calls GET /me/export."""
    app.dependency_overrides[get_current_user] = _override_current_user(_admin_user_dict())
    try:
        response = await client.get("/api/v1/customer/me/export")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
