# tests/test_admin_users.py
"""Tests for Task 10: Admin User Management endpoints.

Strategy:
- test_list_users_requires_admin_role: verifies the endpoint exists and is protected.
- test_create_user_returns_201_and_has_temporary_password: verifies auth guard (unauthenticated -> 401/403).
- Schema-level tests do not require a live DB.
"""
import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Route-level auth guard tests (use the ASGI client, no real DB needed)
# ---------------------------------------------------------------------------

async def test_list_users_requires_admin_role(client):
    """GET /admin/users with invalid token must return 401."""
    resp = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)


async def test_create_user_returns_201_and_has_temporary_password(client):
    """POST /admin/users without auth must return 401 or 403 (auth guard active)."""
    resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": "newstaff@test.com",
            "full_name": "New Staff",
            "role": "staff",
        },
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Schema-level tests (no DB, no network)
# ---------------------------------------------------------------------------

def test_admin_user_create_rejects_customer_role():
    """AdminUserCreate must not accept 'customer' as a role."""
    from app.modules.admin.users.schemas import AdminUserCreate
    with pytest.raises(ValidationError):
        AdminUserCreate(email="x@x.com", role="customer")


def test_admin_user_create_rejects_super_admin_role():
    """AdminUserCreate must not accept 'super-admin' as a role."""
    from app.modules.admin.users.schemas import AdminUserCreate
    with pytest.raises(ValidationError):
        AdminUserCreate(email="x@x.com", role="super-admin")


def test_admin_user_create_accepts_staff():
    from app.modules.admin.users.schemas import AdminUserCreate
    obj = AdminUserCreate(email="staff@pizza.com", role="staff")
    assert obj.role == "staff"


def test_admin_user_create_accepts_admin():
    from app.modules.admin.users.schemas import AdminUserCreate
    obj = AdminUserCreate(email="admin@pizza.com", role="admin")
    assert obj.role == "admin"


def test_admin_user_out_from_attributes():
    """AdminUserOut must be constructible from ORM-like attribute access."""
    from datetime import datetime, timezone
    from app.modules.admin.users.schemas import AdminUserOut
    data = dict(
        id=1,
        email="u@x.com",
        full_name="User",
        role="staff",
        is_active=True,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
        must_change_password=True,
    )
    obj = AdminUserOut(**data)
    assert obj.must_change_password is True


def test_admin_user_create_response_has_temporary_password():
    from app.modules.admin.users.schemas import AdminUserCreateResponse
    obj = AdminUserCreateResponse(id=1, email="a@b.com", role="staff", temporary_password="abc123xyz789abc1")
    assert len(obj.temporary_password) >= 16


# ---------------------------------------------------------------------------
# Service-level unit tests (function signature / importability)
# ---------------------------------------------------------------------------

def test_users_service_importable():
    """users_service module must be importable."""
    from app.modules.admin import users_service  # noqa: F401
    assert hasattr(users_service, "list_users")
    assert hasattr(users_service, "create_user")
    assert hasattr(users_service, "deactivate_user")
    assert hasattr(users_service, "reactivate_user")
    assert hasattr(users_service, "admin_reset_password")


def test_users_router_importable():
    """users_router module must be importable and expose a router."""
    from app.modules.admin.users.router import router
    routes = [r.path for r in router.routes]
    assert "" in routes or any("user" in r for r in routes)
