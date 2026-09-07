"""Tests HTTP bout-en-bout des sessions Super Admin (refresh, logout, révocation).

Couvre les critères d'acceptation du Prompt 04 :
- refresh token rotatif, ancien refresh token refusé après rotation ;
- deux refresh concurrents avec le même token : un seul peut réussir ;
- révocation de session : access token ET refresh token refusés ;
- révocation globale : toutes les sessions précédentes refusées ;
- désactivation du compte : sessions refusées.
"""
import asyncio

import pyotp
import pytest
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.database import get_public_session

pytestmark = pytest.mark.asyncio

_PASSWORD = "correct horse battery staple"


async def _create_mfa_enabled_super_admin(client, email: str) -> str:
    """Crée un compte, active son MFA, retourne le secret TOTP en clair."""
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
    return secret


async def _full_login(client, email: str, secret: str) -> dict:
    resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_refresh_rotates_token_and_rejects_replayed_old_token(client, unique_slug):
    email = f"sess-{unique_slug}@test.com"
    secret = await _create_mfa_enabled_super_admin(client, email)
    tokens = await _full_login(client, email, secret)

    refresh_resp = await client.post(
        "/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    new_tokens = refresh_resp.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # [SECURITE] Rejeu de l'ancien refresh token (deja tourne) -- refuse.
    replay_resp = await client.post(
        "/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay_resp.status_code == 401, replay_resp.text

    # Le nouveau refresh token, lui, fonctionne toujours.
    second_refresh_resp = await client.post(
        "/api/v1/super-admin/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert second_refresh_resp.status_code == 200, second_refresh_resp.text


async def test_concurrent_refresh_with_same_token_only_one_succeeds(client, unique_slug):
    email = f"sess-concurrent-{unique_slug}@test.com"
    secret = await _create_mfa_enabled_super_admin(client, email)
    tokens = await _full_login(client, email, secret)

    results = await asyncio.gather(
        client.post("/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}),
        client.post("/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401], [r.text for r in results]


async def test_logout_revokes_session_access_and_refresh_rejected(client, unique_slug):
    email = f"sess-logout-{unique_slug}@test.com"
    secret = await _create_mfa_enabled_super_admin(client, email)
    tokens = await _full_login(client, email, secret)

    logout_resp = await client.post(
        "/api/v1/super-admin/logout",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert logout_resp.status_code == 204, logout_resp.text

    refresh_resp = await client.post(
        "/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh_resp.status_code == 401, refresh_resp.text

    business_resp = await client.get(
        "/api/v1/admin/tenants",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert business_resp.status_code == 401, business_resp.text


async def test_revoke_all_sessions_rejects_every_previous_session(client, unique_slug):
    email = f"sess-revokeall-{unique_slug}@test.com"
    secret = await _create_mfa_enabled_super_admin(client, email)
    session_a = await _full_login(client, email, secret)
    session_b = await _full_login(client, email, secret)

    resp = await client.post(
        "/api/v1/super-admin/revoke-all-sessions",
        headers={"Authorization": f"Bearer {session_a['access_token']}"},
    )
    assert resp.status_code == 204, resp.text

    for tokens in (session_a, session_b):
        business_resp = await client.get(
            "/api/v1/admin/tenants",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert business_resp.status_code == 401, business_resp.text

        refresh_resp = await client.post(
            "/api/v1/super-admin/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_resp.status_code == 401, refresh_resp.text


async def test_disabled_super_admin_sessions_are_rejected(client, unique_slug):
    """[SECURITE] Desactiver le compte (is_active=false, quel que soit le
    mecanisme -- pas d'endpoint dedie dans ce correctif) doit invalider
    immediatement toute session active, sans attendre l'expiration du token."""
    email = f"sess-disabled-{unique_slug}@test.com"
    secret = await _create_mfa_enabled_super_admin(client, email)
    tokens = await _full_login(client, email, secret)

    async with get_public_session() as session:
        await session.execute(
            text("UPDATE public.super_admins SET is_active = false WHERE email = :email"),
            {"email": email},
        )
        await session.commit()

    business_resp = await client.get(
        "/api/v1/admin/tenants",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert business_resp.status_code == 401, business_resp.text
