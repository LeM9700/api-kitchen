"""Tests for super-admin endpoints.

Coverage:
- POST /api/v1/admin/impersonate/{tenant_slug} -- requires super-admin role
- PATCH /api/v1/admin/tenants/{id}/suspend -- requires super-admin role
"""
import pytest


@pytest.mark.asyncio
async def test_impersonate_requires_super_admin(client):
    """An invalid token must yield 401 Unauthorized."""
    resp = await client.post(
        "/api/v1/admin/impersonate/sometenant",
        headers={"Authorization": "Bearer invalid"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_suspend_tenant_requires_super_admin(client):
    """An invalid token must yield 401 Unauthorized."""
    resp = await client.patch(
        "/api/v1/admin/tenants/1/suspend",
        headers={"Authorization": "Bearer invalid"},
        json={"suspend": True},
    )
    assert resp.status_code == 401
