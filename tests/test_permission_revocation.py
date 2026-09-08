"""Prompt 06 — permissions explicites, révocation de sessions et refresh atomique.

Couvre :
- un staff sans permissions explicites (permissions=[] par défaut) ne peut agir ;
- un changement de permissions s'applique immédiatement, même sur un access
  token déjà émis (relecture live en base plutôt que confiance au JWT) ;
- un changement de permissions / une désactivation révoquent les sessions
  (refresh tokens) actives ;
- une désactivation invalide immédiatement l'access token en circulation ;
- le refresh token revérifie is_active et résiste à une rotation concurrente.
"""

import asyncio

import pytest


async def _register_admin(client, tenant_slug: str) -> tuple[str, str]:
    email = f"admin-{tenant_slug}@test.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_slug,
            "email": email,
            "password": "Valid1!aa",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"], email


async def _create_staff(client, admin_token: str, tenant_slug: str, email: str) -> dict:
    resp = await client.post(
        "/api/v1/admin/users",
        json={"email": email, "full_name": "Staff Member", "role": "staff"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login_staff(client, tenant_slug: str, email: str, password: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_new_staff_without_explicit_permissions_cannot_act(client, unique_slug):
    """Un staff fraîchement créé (permissions non spécifiées) ne peut accéder à
    aucune route gated par require_permission -- deny by default."""
    tenant_slug = f"perm{unique_slug}"
    admin_token, _ = await _register_admin(client, tenant_slug)
    staff_email = f"staff-{unique_slug}@test.com"
    created = await _create_staff(client, admin_token, tenant_slug, staff_email)
    assert created["temporary_password"]

    # must_change_password bloque tout sauf /auth/change-password : on change
    # le mot de passe pour obtenir un token pleinement utilisable.
    login = await _login_staff(client, tenant_slug, staff_email, created["temporary_password"])
    staff_headers = {"Authorization": f"Bearer {login['access_token']}"}
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "NewValid1!aa"},
        headers=staff_headers,
    )
    assert change.status_code == 200, change.text

    login2 = await _login_staff(client, tenant_slug, staff_email, "NewValid1!aa")
    staff_headers = {"Authorization": f"Bearer {login2['access_token']}"}

    resp = await client.get("/api/v1/stock/ingredients", headers=staff_headers)
    assert resp.status_code == 403


async def test_permission_grant_takes_effect_on_already_issued_access_token(client, unique_slug):
    """Un access token émis AVANT l'octroi d'une permission doit pouvoir
    l'exercer immédiatement après -- la relecture live en base (pas le JWT)
    fait foi (voir app.core.tenancy.tenant.get_live_tenant_user_state)."""
    tenant_slug = f"livep{unique_slug}"
    admin_token, _ = await _register_admin(client, tenant_slug)
    staff_email = f"staff-{unique_slug}@test.com"
    created = await _create_staff(client, admin_token, tenant_slug, staff_email)

    login = await _login_staff(client, tenant_slug, staff_email, created["temporary_password"])
    staff_headers = {"Authorization": f"Bearer {login['access_token']}"}
    await client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "NewValid1!aa"},
        headers=staff_headers,
    )
    login2 = await _login_staff(client, tenant_slug, staff_email, "NewValid1!aa")
    old_access_token = login2["access_token"]
    staff_headers = {"Authorization": f"Bearer {old_access_token}"}

    denied = await client.get("/api/v1/stock/ingredients", headers=staff_headers)
    assert denied.status_code == 403

    grant = await client.patch(
        f"/api/v1/admin/users/{created['id']}/permissions",
        json={"permissions": ["stock:read"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert grant.status_code == 200, grant.text

    # Même token, jamais réémis : la permission doit maintenant être active.
    allowed = await client.get("/api/v1/stock/ingredients", headers=staff_headers)
    assert allowed.status_code == 200, allowed.text


async def test_permission_update_revokes_active_refresh_tokens(client, unique_slug):
    """PATCH .../permissions révoque les sessions (refresh tokens) actives."""
    tenant_slug = f"permrt{unique_slug}"
    admin_token, _ = await _register_admin(client, tenant_slug)
    staff_email = f"staff-{unique_slug}@test.com"
    created = await _create_staff(client, admin_token, tenant_slug, staff_email)

    login = await _login_staff(client, tenant_slug, staff_email, created["temporary_password"])
    refresh_token = login["refresh_token"]

    grant = await client.patch(
        f"/api/v1/admin/users/{created['id']}/permissions",
        json={"permissions": ["stock:read"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert grant.status_code == 200, grant.text

    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 401, refreshed.text


async def test_deactivate_rejects_already_issued_access_token(client, unique_slug):
    """Un compte désactivé voit son access token déjà émis rejeté immédiatement."""
    tenant_slug = f"deact{unique_slug}"
    admin_token, _ = await _register_admin(client, tenant_slug)
    staff_email = f"staff-{unique_slug}@test.com"
    created = await _create_staff(client, admin_token, tenant_slug, staff_email)

    login = await _login_staff(client, tenant_slug, staff_email, created["temporary_password"])
    staff_headers = {"Authorization": f"Bearer {login['access_token']}"}
    await client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "NewValid1!aa"},
        headers=staff_headers,
    )
    login2 = await _login_staff(client, tenant_slug, staff_email, "NewValid1!aa")
    staff_headers = {"Authorization": f"Bearer {login2['access_token']}"}

    me_before = await client.get("/api/v1/auth/me", headers=staff_headers)
    assert me_before.status_code == 200, me_before.text

    deactivate = await client.patch(
        f"/api/v1/admin/users/{created['id']}/deactivate",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deactivate.status_code == 200, deactivate.text

    me_after = await client.get("/api/v1/auth/me", headers=staff_headers)
    assert me_after.status_code == 401


async def test_refresh_rejects_disabled_account(client, unique_slug):
    """Le refresh doit revérifier is_active même si le refresh token n'a pas expiré."""
    tenant_slug = f"refdis{unique_slug}"
    admin_token, _ = await _register_admin(client, tenant_slug)
    staff_email = f"staff-{unique_slug}@test.com"
    created = await _create_staff(client, admin_token, tenant_slug, staff_email)

    login = await _login_staff(client, tenant_slug, staff_email, created["temporary_password"])
    refresh_token = login["refresh_token"]

    deactivate = await client.patch(
        f"/api/v1/admin/users/{created['id']}/deactivate",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deactivate.status_code == 200, deactivate.text

    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 401, refreshed.text


async def test_concurrent_refresh_with_same_token_only_one_succeeds(client, unique_slug):
    """Deux requêtes /auth/refresh concurrentes avec le MÊME refresh token :
    une seule doit réussir (rotation atomique, voir app.modules.auth.service.refresh_token)."""
    tenant_slug = f"concref{unique_slug}"
    admin_token, email = await _register_admin(client, tenant_slug)
    login = await _login_staff(client, tenant_slug, email, "Valid1!aa")
    refresh_token = login["refresh_token"]

    results = await asyncio.gather(
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401], [r.text for r in results]
