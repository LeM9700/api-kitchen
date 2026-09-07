"""Tests for super-admin endpoints.

Coverage:
- POST /api/v1/admin/impersonate/{tenant_slug} -- requires super-admin role
- PATCH /api/v1/admin/tenants/{id}/suspend -- requires super-admin role
- Flux d'impersonation complet (Prompt 14) -- corrige le bug historique ou
  le token (sub="0" + tenant_slug non-null) tombait dans la branche
  user_belongs_to_tenant(0, tenant_slug, ...) de get_current_user et etait
  donc TOUJOURS rejete : aucun test ne couvrait jusqu'ici le succes d'un
  appel reellement authentifie avec ce token.
"""
from datetime import timedelta

import pyotp
import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import text

from app.core.auth.security import create_access_token, get_password_hash
from app.core.config import settings
from app.core.database import get_public_session
from app.main import app as fastapi_app

pytestmark = pytest.mark.asyncio

_PASSWORD = "correct horse battery staple"


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


# ---------------------------------------------------------------------------
# Flux d'impersonation complet
# ---------------------------------------------------------------------------


async def _create_full_super_admin_session(client, email: str) -> dict:
    """Cree un compte super-admin, active son MFA, retourne une session complete
    (access_token/refresh_token) prete a appeler /admin/impersonate/*."""
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, :hash, true)"
            ),
            {"email": email, "hash": get_password_hash(_PASSWORD)},
        )
        await session.commit()

    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    enrollment_token = login_resp.json()["access_token"]
    setup_resp = await client.post(
        "/api/v1/super-admin/mfa/setup",
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    secret = setup_resp.json()["secret"]
    await client.post(
        "/api/v1/super-admin/mfa/confirm",
        json={"totp_code": pyotp.TOTP(secret).now()},
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )

    full_login_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )
    assert full_login_resp.status_code == 200, full_login_resp.text
    return full_login_resp.json()


async def test_impersonation_full_flow_grants_scoped_read_access(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Le coeur du correctif Prompt 14 : un appel authentifie avec
    le token d'impersonation doit reussir (avant correctif : 401 systematique,
    voir docstring module). Les permissions restent minimales : lecture OK,
    ecriture refusee (voir IMPERSONATION_PERMISSIONS)."""
    email = f"imp-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    resp = await client.post(
        f"/api/v1/admin/impersonate/{demo_tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "refresh_token" not in body or body.get("refresh_token") is None
    impersonation_token = body["access_token"]

    read_resp = await client.get(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert read_resp.status_code == 200, read_resp.text

    write_resp = await client.patch(
        "/api/v1/orders/1/status",
        json={"status": "confirmed"},
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert write_resp.status_code == 403, write_resp.text


async def test_impersonation_rejects_unknown_tenant(client, unique_slug):
    email = f"imp-404-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    resp = await client.post(
        f"/api/v1/admin/impersonate/nonexistent-tenant-{unique_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert resp.status_code == 404, resp.text


async def test_impersonation_token_is_strictly_tenant_pinned(
    client, unique_slug, demo_tenant_slug
):
    email = f"imp-pin-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    resp = await client.post(
        f"/api/v1/admin/impersonate/{demo_tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    token = resp.json()["access_token"]

    import jwt as pyjwt

    payload = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert payload["tenant_slug"] == demo_tenant_slug
    assert payload["impersonation"] is True
    assert payload["sub"] != "0"  # [SECURITE] plus jamais le sentinel historique
    assert len(payload["permissions"]) > 0  # liste explicite, jamais None/illimitee
    assert "*" not in payload["permissions"]
    assert not any(perm.endswith(":write") for perm in payload["permissions"])


async def test_impersonation_expired_token_is_rejected(client, unique_slug, demo_tenant_slug):
    email = f"imp-expired-{unique_slug}@test.com"
    await _create_full_super_admin_session(client, email)

    async with get_public_session() as session:
        admin_row = await session.execute(
            text("SELECT id, auth_version FROM public.super_admins WHERE email = :email"),
            {"email": email},
        )
        admin_id, auth_version = admin_row.first()

    from app.core.auth.impersonation import IMPERSONATION_PERMISSIONS

    expired_token = create_access_token(
        {
            "sub": str(admin_id),
            "email": f"impersonation:{email}->{demo_tenant_slug}",
            "role": "admin",
            "tenant_slug": demo_tenant_slug,
            "tenant_id": 1,
            "impersonation": True,
            "impersonated_by_super_admin_id": admin_id,
            "impersonated_by_email": email,
            "source_sid": "does-not-matter",
            "auth_version": auth_version,
            "permissions": IMPERSONATION_PERMISSIONS,
        },
        expires_delta=timedelta(seconds=-10),
    )

    resp = await client.get(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401, resp.text


async def test_impersonation_rejected_when_source_session_revoked(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] "session source revoquee : impersonation refusee" -- meme si
    le token d'impersonation lui-meme n'a pas expire."""
    email = f"imp-srcrevoked-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    impersonate_resp = await client.post(
        f"/api/v1/admin/impersonate/{demo_tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    impersonation_token = impersonate_resp.json()["access_token"]

    logout_resp = await client.post(
        "/api/v1/super-admin/logout",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert logout_resp.status_code == 204, logout_resp.text

    resp = await client.get(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 401, resp.text


async def test_impersonation_rejected_when_issuer_globally_revoked(
    client, unique_slug, demo_tenant_slug
):
    email = f"imp-globalrevoke-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    impersonate_resp = await client.post(
        f"/api/v1/admin/impersonate/{demo_tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    impersonation_token = impersonate_resp.json()["access_token"]

    revoke_resp = await client.post(
        "/api/v1/super-admin/revoke-all-sessions",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert revoke_resp.status_code == 204, revoke_resp.text

    resp = await client.get(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 401, resp.text


async def test_impersonation_end_revokes_token_immediately(client, unique_slug, demo_tenant_slug):
    """[SECURITE] "token revoque ... refuse" -- via /impersonation/end + deny-list
    Redis, verifiee ici avec un vrai client Redis branche sur app.state."""
    redis_client = redis_asyncio.from_url(settings.redis_url)
    fastapi_app.state.arq_pool = redis_client
    try:
        email = f"imp-end-{unique_slug}@test.com"
        admin_session = await _create_full_super_admin_session(client, email)

        impersonate_resp = await client.post(
            f"/api/v1/admin/impersonate/{demo_tenant_slug}",
            headers={"Authorization": f"Bearer {admin_session['access_token']}"},
        )
        impersonation_token = impersonate_resp.json()["access_token"]

        end_resp = await client.post(
            "/api/v1/admin/impersonation/end",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )
        assert end_resp.status_code == 204, end_resp.text

        resp = await client.get(
            "/api/v1/orders",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )
        assert resp.status_code == 401, resp.text
    finally:
        fastapi_app.state.arq_pool = None
        await redis_client.aclose()


async def test_impersonation_open_and_close_are_audited(client, unique_slug, demo_tenant_slug):
    email = f"imp-audit-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)

    impersonate_resp = await client.post(
        f"/api/v1/admin/impersonate/{demo_tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    impersonation_token = impersonate_resp.json()["access_token"]

    await client.post(
        "/api/v1/admin/impersonation/end",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )

    async with get_public_session() as session:
        rows = await session.execute(
            text(
                "SELECT event_type, actor_email, target_id, metadata "
                "FROM public.platform_audit_logs "
                "WHERE actor_email IN (:email, :imp_email) "
                "ORDER BY created_at"
            ),
            {"email": email, "imp_email": email},
        )
        events = [dict(r._mapping) for r in rows]

    event_types = [e["event_type"] for e in events]
    assert "impersonation_started" in event_types
    assert "impersonation_ended" in event_types
    for event in events:
        assert event["target_id"] == demo_tenant_slug or event["target_id"] is None
        if event["metadata"]:
            assert "password" not in event["metadata"]
            assert "secret" not in event["metadata"]


async def test_standard_tenant_jwt_unaffected_by_impersonation_branch(authed_client):
    """[SECURITE] Regression -- un JWT tenant standard doit toujours passer
    par user_belongs_to_tenant, jamais par la branche impersonation."""
    resp = await authed_client.get("/api/v1/auth/me")
    assert resp.status_code == 200, resp.text
