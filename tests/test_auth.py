import pytest
from app.core.database import tenant_schema_name
from app.core.http.errors import AppError
from app.modules.auth.schemas import LoginRequest, RegisterRequest, UserOut
from pydantic import ValidationError


def test_tenant_schema_name_rejects_injection():
    with pytest.raises(AppError) as exc:
        tenant_schema_name('x"; DROP SCHEMA public; --')
    assert exc.value.code == "INVALID_SLUG"
    assert exc.value.status_code == 400


def test_tenant_schema_name_accepts_valid_slug():
    assert tenant_schema_name("pizza-roma") == "tenant_pizza-roma"
    assert tenant_schema_name("a") == "tenant_a"


def test_tenant_schema_name_rejects_uppercase():
    with pytest.raises(AppError):
        tenant_schema_name("PizzaRoma")


def test_register_request_rejects_bad_slug():
    with pytest.raises(ValidationError) as exc:
        RegisterRequest(
            tenant_slug="INVALID SLUG!",
            tenant_name="Test",
            email="a@b.com",
            password="Valid1!aa",
        )
    assert "slug" in str(exc.value).lower() or "INVALID_SLUG" in str(exc.value)


async def test_me_requires_auth(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_me_returns_email_verified_field(client):
    # Register + login to get a token
    reg = await client.post("/api/v1/auth/register", json={
        "tenant_slug": "testme",
        "tenant_name": "Test Me",
        "email": "me@test.com",
        "password": "Valid1!aa",
    })
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "testme"}

    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "email_verified" in data
    assert data["email_verified"] is False  # not verified yet at registration
    assert "must_change_password" in data


def test_user_model_has_auth_extension_columns():
    """Test that User model has password reset and must_change_password columns."""
    from app.modules.auth.models import User

    cols = {c.key for c in User.__mapper__.columns}
    assert "password_reset_token" in cols
    assert "password_reset_expires_at" in cols
    assert "must_change_password" in cols
    assert "mfa_secret" in cols
    assert "mfa_enabled" in cols
    assert "mfa_backup_codes" in cols


def test_refresh_token_model_has_device_columns():
    from app.modules.auth.models import RefreshToken

    cols = {c.key for c in RefreshToken.__mapper__.columns}
    assert "user_agent" in cols
    assert "ip_address" in cols


def test_login_request_accepts_optional_mfa_code():
    req = LoginRequest(
        tenant_slug="test",
        email="admin@test.com",
        password="Valid1!aa",
        mfa_code="123456",
    )
    assert req.mfa_code == "123456"


def test_user_out_does_not_expose_mfa_fields():
    fields = set(UserOut.model_fields)
    assert "mfa_secret" not in fields
    assert "mfa_backup_codes" not in fields


def test_mfa_totp_helper_accepts_valid_code():
    import pyotp
    from app.modules.auth import service

    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    assert service._verify_totp(secret, code) is True


async def test_mfa_backup_code_is_consumed_once():
    import pyotp

    from app.core.auth.security import get_password_hash
    from app.modules.auth import service
    from app.modules.auth.models import User

    class FakeSession:
        async def flush(self):
            return None

    user = User(
        id=1,
        email="root@test.com",
        password_hash="x",
        role="super-admin",
        mfa_secret=pyotp.random_base32(),
        mfa_enabled=True,
        mfa_backup_codes=[get_password_hash("BACKUP123")],
    )

    await service._verify_login_mfa(FakeSession(), user, "BACKUP123")
    assert user.mfa_backup_codes == []

    with pytest.raises(AppError) as exc:
        await service._verify_login_mfa(FakeSession(), user, "BACKUP123")
    assert exc.value.code == "INVALID_MFA_CODE"


async def test_forgot_password_returns_202_for_unknown_email(client):
    """Doit retourner 202 meme si l'email n'existe pas (anti-enumeration)."""
    resp = await client.post("/api/v1/auth/forgot-password", json={
        "email": "unknown@nowhere.com",
        "tenant_slug": "nonexistent",
    })
    assert resp.status_code == 202


async def test_reset_password_rejects_invalid_token(client):
    resp = await client.post("/api/v1/auth/reset-password", json={
        "email": "test@test.com",
        "tenant_slug": "test",
        "token": "WRONGTOKEN",
        "new_password": "NewPass1!",
    })
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_TOKEN"


async def test_resend_verification_requires_auth(client):
    resp = await client.post("/api/v1/auth/resend-verification")
    assert resp.status_code == 401


async def test_change_password_requires_auth(client):
    resp = await client.post("/api/v1/auth/change-password", json={
        "new_password": "NewPass1!",
    })
    assert resp.status_code == 401


async def test_get_sessions_requires_auth(client):
    resp = await client.get("/api/v1/auth/sessions")
    assert resp.status_code == 401


async def test_get_sessions_returns_list(client):
    # register + get token
    reg = await client.post("/api/v1/auth/register", json={
        "tenant_slug": "sesstest",
        "tenant_name": "Sess Test",
        "email": "sess@test.com",
        "password": "Valid1!aa",
    })
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    session_id = reg.json()["session_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Slug": "sesstest"}

    resp = await client.get(f"/api/v1/auth/sessions?current_session_id={session_id}", headers=headers)
    assert resp.status_code == 200
    sessions = resp.json()
    assert isinstance(sessions, list)
    assert len(sessions) >= 1
    assert any(s["is_current"] for s in sessions)
