"""Tests for authenticated tenant establishment context."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.http.deps import get_current_user
from app.main import app
from app.modules.admin.tenants import service as tenant_service


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


def _user(role: str, user_id: str = "7"):
    async def override() -> dict:
        return {
            "id": user_id,
            "tenant_id": 1,
            "tenant_slug": "default",
            "role": role,
            "email": f"{role}@example.test",
        }

    return override


async def test_tenant_establishments_requires_auth(client):
    response = await client.get("/api/v1/tenant/establishments")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_tenant_establishments_rejects_customer(client):
    app.dependency_overrides[get_current_user] = _user("customer")
    try:
        response = await client.get("/api/v1/tenant/establishments")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_tenant_establishments_route_returns_accessible_items(client, monkeypatch):
    app.dependency_overrides[get_current_user] = _user("staff")

    @asynccontextmanager
    async def fake_tenant_session(tenant_slug: str):
        assert tenant_slug == "default"
        yield SimpleNamespace()

    async def fake_list_accessible_establishments(_session, current_user):
        assert current_user["role"] == "staff"
        return [
            SimpleNamespace(
                id=4,
                name="Le Patio Centre",
                timezone="Europe/Paris",
                is_active=True,
            )
        ]

    monkeypatch.setattr("app.modules.admin.tenants.router.get_tenant_session", fake_tenant_session)
    monkeypatch.setattr(
        "app.modules.admin.tenants.router.tenant_service.list_accessible_establishments",
        fake_list_accessible_establishments,
    )

    try:
        response = await client.get("/api/v1/tenant/establishments")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 4,
            "name": "Le Patio Centre",
            "timezone": "Europe/Paris",
            "is_active": True,
        }
    ]


async def test_list_accessible_establishments_admin_lists_all():
    establishments = [
        SimpleNamespace(id=1, name="A", timezone="Europe/Paris", is_active=True),
        SimpleNamespace(id=2, name="B", timezone="Europe/Paris", is_active=False),
    ]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult(establishments)),
        scalar=AsyncMock(),
        get=AsyncMock(),
    )

    result = await tenant_service.list_accessible_establishments(session, {"role": "admin", "id": "9"})

    assert result == establishments
    session.execute.assert_awaited_once()
    session.scalar.assert_not_awaited()
    session.get.assert_not_awaited()


async def test_list_accessible_establishments_staff_uses_active_profile():
    establishment = SimpleNamespace(id=3, name="Kiosque", timezone="Europe/Paris", is_active=True)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(establishment_id=3)),
        get=AsyncMock(return_value=establishment),
    )

    result = await tenant_service.list_accessible_establishments(session, {"role": "staff", "id": "7"})

    assert result == [establishment]
    session.scalar.assert_awaited_once()
    session.get.assert_awaited_once()


async def test_list_accessible_establishments_staff_without_profile_returns_empty():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(),
    )

    result = await tenant_service.list_accessible_establishments(session, {"role": "staff", "id": "7"})

    assert result == []
    session.scalar.assert_awaited_once()
    session.get.assert_not_awaited()


async def test_list_accessible_establishments_staff_inactive_establishment_returns_empty():
    establishment = SimpleNamespace(id=3, name="Kiosque", timezone="Europe/Paris", is_active=False)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(establishment_id=3)),
        get=AsyncMock(return_value=establishment),
    )

    result = await tenant_service.list_accessible_establishments(session, {"role": "staff", "id": "7"})

    assert result == []
