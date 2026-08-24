"""Regression tests for MFA consistency between the admin/super-admin roles.

Couvre le finding F-05 / FF-06bis / P1-01 : le routeur autorisait deja
``role in ("super-admin", "admin")`` sur les routes MFA, mais la couche
service bloquait encore tout role different de ``super-admin`` avec un 403,
et le login ne verifiait jamais le code TOTP pour un admin. Ces tests
verrouillent le comportement corrige.
"""

import pyotp


async def _register_admin(client, tenant_slug: str) -> tuple[str, str]:
    """Enregistre un nouveau tenant et retourne (access_token, email)."""
    email = f"{tenant_slug}@test.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_slug,
            "email": email,
            "password": "Valid1!aa",
        },
    )
    if resp.status_code == 201:
        return resp.json()["access_token"], email

    assert resp.status_code == 409, resp.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": "Valid1!aa"},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"], email


async def test_admin_can_setup_mfa(client):
    """Un compte 'admin' (role attribue au premier utilisateur d'un tenant)
    ne doit plus recevoir 403 sur /mfa/setup."""
    token, _ = await _register_admin(client, "mfasetup")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "mfasetup"}

    resp = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "secret" in body
    assert "otpauth_uri" in body
    assert "backup_codes" in body


async def test_admin_can_confirm_mfa(client):
    """Un admin doit pouvoir confirmer le MFA avec un code TOTP valide."""
    token, _ = await _register_admin(client, "mfaconfirm")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "mfaconfirm"}

    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]

    totp_code = pyotp.TOTP(secret).now()
    confirm = await client.post(
        "/api/v1/auth/mfa/confirm",
        json={"totp_code": totp_code},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text


async def test_admin_login_requires_mfa_code_once_enabled(client):
    """Une fois le MFA active pour un admin, le login sans code doit etre
    refuse (MFA_REQUIRED) et le login avec le bon code doit reussir."""
    token, email = await _register_admin(client, "mfalogin")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "mfalogin"}

    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    secret = setup.json()["secret"]
    totp_code = pyotp.TOTP(secret).now()
    confirm = await client.post(
        "/api/v1/auth/mfa/confirm",
        json={"totp_code": totp_code},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text

    # Login sans code MFA -> refuse.
    no_code = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": "mfalogin", "email": email, "password": "Valid1!aa"},
    )
    assert no_code.status_code == 401
    assert no_code.json()["code"] == "MFA_REQUIRED"

    # Login avec le bon code TOTP -> reussit.
    good_code = pyotp.TOTP(secret).now()
    with_code = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_slug": "mfalogin",
            "email": email,
            "password": "Valid1!aa",
            "mfa_code": good_code,
        },
    )
    assert with_code.status_code == 200, with_code.text
    assert "access_token" in with_code.json()


async def test_admin_can_regenerate_mfa_backup_codes(client):
    """Un admin avec MFA deja active doit pouvoir regenerer ses backup codes."""
    token, _ = await _register_admin(client, "mfabackup")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "mfabackup"}

    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    secret = setup.json()["secret"]
    totp_code = pyotp.TOTP(secret).now()
    await client.post("/api/v1/auth/mfa/confirm", json={"totp_code": totp_code}, headers=headers)

    regen_code = pyotp.TOTP(secret).now()
    resp = await client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"totp_code": regen_code},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert "backup_codes" in resp.json()
