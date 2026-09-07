"""Tests HTTP bout-en-bout du flux MFA Super Admin.

Couvre les critères d'acceptation du Prompt 04 (durcissement de
l'authentification Super Admin) :
- mot de passe seul insuffisant dès que le MFA est activé ;
- migration progressive contrôlée : un compte actif sans MFA reçoit un token
  d'enrôlement restreint, jamais un accès complet ;
- TOTP valide accepté, invalide refusé ;
- code de récupération à usage unique ;
- secret TOTP jamais stocké en clair.
"""
import pyotp
import pytest
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.database import get_public_session

pytestmark = pytest.mark.asyncio

_PASSWORD = "correct horse battery staple"


async def _create_super_admin(email: str, password: str = _PASSWORD) -> int:
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, :hash, true) RETURNING id"
            ),
            {"email": email, "hash": get_password_hash(password)},
        )
        admin_id = result.scalar_one()
        await session.commit()
        return admin_id


async def test_login_without_mfa_issues_enrollment_token_only(client, unique_slug):
    email = f"mfa-{unique_slug}@test.com"
    await _create_super_admin(email)

    resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mfa_setup_required"] is True
    assert body["refresh_token"] is None


async def test_enrollment_token_cannot_call_business_route(client, unique_slug):
    """[SECURITE] Le token d'enrolement ne doit ouvrir AUCUNE capacite metier --
    role="super-admin-enrollment" ne matche pas require_role("super-admin")."""
    email = f"mfa-scope-{unique_slug}@test.com"
    await _create_super_admin(email)
    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    token = login_resp.json()["access_token"]

    resp = await client.get(
        "/api/v1/admin/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_mfa_setup_confirm_then_login_requires_totp(client, unique_slug):
    email = f"mfa-full-{unique_slug}@test.com"
    await _create_super_admin(email)

    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    enrollment_token = login_resp.json()["access_token"]

    setup_resp = await client.post(
        "/api/v1/super-admin/mfa/setup",
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    assert setup_resp.status_code == 200, setup_resp.text
    secret = setup_resp.json()["secret"]
    assert len(setup_resp.json()["recovery_codes"]) == 10

    confirm_resp = await client.post(
        "/api/v1/super-admin/mfa/confirm",
        json={"totp_code": pyotp.TOTP(secret).now()},
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    # [SECURITE] Mot de passe seul desormais refuse.
    pwd_only_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    assert pwd_only_resp.status_code == 401, pwd_only_resp.text
    assert pwd_only_resp.json()["code"] == "MFA_REQUIRED"

    # Code TOTP invalide refuse.
    bad_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": "000000"},
    )
    assert bad_resp.status_code == 401, bad_resp.text

    # TOTP valide accepte -> session complete avec refresh token.
    ok_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    body = ok_resp.json()
    assert body["mfa_setup_required"] is False
    assert body["refresh_token"] is not None


async def test_recovery_code_is_single_use(client, unique_slug):
    email = f"mfa-recovery-{unique_slug}@test.com"
    await _create_super_admin(email)

    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    enrollment_token = login_resp.json()["access_token"]
    setup_resp = await client.post(
        "/api/v1/super-admin/mfa/setup",
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    secret = setup_resp.json()["secret"]
    recovery_code = setup_resp.json()["recovery_codes"][0]
    await client.post(
        "/api/v1/super-admin/mfa/confirm",
        json={"totp_code": pyotp.TOTP(secret).now()},
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )

    first_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": recovery_code},
    )
    assert first_resp.status_code == 200, first_resp.text

    second_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": recovery_code},
    )
    assert second_resp.status_code == 401, second_resp.text


async def test_mfa_secret_never_stored_in_plaintext(client, unique_slug):
    email = f"mfa-encrypted-{unique_slug}@test.com"
    admin_id = await _create_super_admin(email)

    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    enrollment_token = login_resp.json()["access_token"]
    setup_resp = await client.post(
        "/api/v1/super-admin/mfa/setup",
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    secret = setup_resp.json()["secret"]

    async with get_public_session() as session:
        row = await session.execute(
            text("SELECT mfa_secret_encrypted FROM public.super_admins WHERE id = :id"),
            {"id": admin_id},
        )
        stored = row.scalar_one()

    assert stored != secret
    assert secret not in stored


async def test_disabled_account_login_is_rejected(client, unique_slug):
    email = f"mfa-disabled-{unique_slug}@test.com"
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, :hash, false)"
            ),
            {"email": email, "hash": get_password_hash(_PASSWORD)},
        )
        await session.commit()

    resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    assert resp.status_code == 401, resp.text
