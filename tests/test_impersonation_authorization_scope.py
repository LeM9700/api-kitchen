"""Tests -- deny-by-default de l'impersonation sur les routes protégées
uniquement par rôle (Correction 1, suite du durcissement Super Admin).

Contexte : un token d'impersonation porte ``role="admin"`` pour rester
compatible avec le modèle de rôles tenant (voir
app.core.auth.impersonation.create_impersonation_token). Avant ce correctif,
``require_role("admin")`` seul ne consultait jamais ``is_impersonation`` --
n'importe quelle route protégée par rôle seul (pas de permission fine)
héritait donc de la pleine capacité du rôle "admin" pendant une impersonation,
malgré la liste ``IMPERSONATION_PERMISSIONS`` posée à l'émission du token.

Correctif (app.core.http.deps.require_role) : un token d'impersonation est
désormais refusé PAR ``require_role()`` LUI-MÊME, avant même de comparer les
rôles listés -- deny by default, sans flag d'opt-in par route. Seules les
routes gated par ``require_permission(...)`` avec une permission de
``IMPERSONATION_PERMISSIONS`` peuvent accepter une impersonation (voir
``has_permission()``).
"""
import pyotp
import pytest
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.database import get_public_session

pytestmark = pytest.mark.asyncio

_PASSWORD = "correct horse battery staple"


async def _create_full_super_admin_session(client, email: str) -> dict:
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


async def _impersonate(client, admin_session: dict, tenant_slug: str) -> str:
    resp = await client.post(
        f"/api/v1/admin/impersonate/{tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_impersonation_rejected_on_role_only_admin_route(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Coeur de la Correction 1 : GET /admin/tenant/config n'est
    protege que par require_role("admin"), sans permission fine -- un token
    d'impersonation doit etre refuse la, meme s'il porte role="admin"."""
    email = f"scope-role-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    impersonation_token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.get(
        "/api/v1/admin/tenant/config",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_impersonation_rejected_on_super_admin_route(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Une route Super Admin (require_role("super-admin")) refuse
    toujours l'impersonation -- deny-by-default s'applique avant meme la
    comparaison de roles."""
    email = f"scope-sa-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    impersonation_token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.get(
        "/api/v1/admin/tenants",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_impersonation_rejected_on_role_only_mutation_route(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Token refuse sur une mutation (PATCH) protegee par role seul."""
    email = f"scope-mut-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    impersonation_token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.patch(
        "/api/v1/admin/tenant/config",
        json={"is_temporarily_closed": True},
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_impersonation_rejected_on_permission_gated_write_route(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Meme gated par require_permission (pas require_role seul),
    une permission d'ecriture (hors IMPERSONATION_PERMISSIONS) reste refusee."""
    email = f"scope-write-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    impersonation_token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.patch(
        "/api/v1/orders/1/status",
        json={"status": "confirmed"},
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_impersonation_accepted_on_explicit_read_permission_route(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Seule route legitimement accessible : require_permission
    avec une permission listee dans IMPERSONATION_PERMISSIONS (lecture)."""
    email = f"scope-read-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    impersonation_token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.get(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {impersonation_token}"},
    )
    assert resp.status_code == 200, resp.text


async def test_normal_admin_token_unaffected_by_impersonation_deny(authed_client):
    """[SECURITE] Regression -- un vrai token admin (pas une impersonation)
    doit toujours passer require_role("admin") normalement."""
    resp = await authed_client.get("/api/v1/admin/tenant/config")
    assert resp.status_code == 200, resp.text
