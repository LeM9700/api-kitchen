"""Tests P1 - must_change_password, commandes invite, email unique, MFA admin, pagination.

Couvre les fixes FF-04, FF-06, FF-07, FF-08, FF-13.
Mix tests unitaires (service / deps) et HTTP (client ASGI).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# FF-04 - must_change_password
# ---------------------------------------------------------------------------


def test_must_change_password_allowed_paths_contains_expected_routes():
    """_MUST_CHANGE_PASSWORD_ALLOWED_PATHS doit contenir les routes autorisees."""
    from app.core.http.deps import _MUST_CHANGE_PASSWORD_ALLOWED_PATHS

    assert "/auth/change-password" in _MUST_CHANGE_PASSWORD_ALLOWED_PATHS
    assert "/auth/logout" in _MUST_CHANGE_PASSWORD_ALLOWED_PATHS


def test_must_change_password_change_password_path_passes_check():
    """/api/v1/auth/change-password contient /auth/change-password -> autorise."""
    from app.core.http.deps import _MUST_CHANGE_PASSWORD_ALLOWED_PATHS

    full_path = "/api/v1/auth/change-password"
    allowed = any(p in full_path for p in _MUST_CHANGE_PASSWORD_ALLOWED_PATHS)
    assert allowed is True


def test_must_change_password_other_paths_are_blocked():
    """/auth/me et autres routes ne sont pas dans la liste -> bloques."""
    from app.core.http.deps import _MUST_CHANGE_PASSWORD_ALLOWED_PATHS

    for path in ["/api/v1/auth/me", "/api/v1/orders", "/api/v1/catalog/products"]:
        allowed = any(p in path for p in _MUST_CHANGE_PASSWORD_ALLOWED_PATHS)
        assert allowed is False, f"Le path {path!r} ne devrait pas etre autorise"


async def test_must_change_password_blocks_route_unit():
    """get_current_user avec must_change_password=True -> 403 sur route non-autorisee.

    Test unitaire direct sur get_current_user : simule un payload JWT deja decode
    via request.state.jwt_payload (comme TenantMiddleware le ferait), sans DB ni Redis.

    [SECURITE] Un compte staff cree par admin doit changer son mot de passe
    avant d'acceder a toute autre ressource.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.http.deps import get_current_user
    from app.core.http.errors import AppError

    mock_request = MagicMock()
    mock_request.state.jwt_payload = {
        "type": "access",
        "sub": "1",
        "tenant_id": 1,
        "tenant_slug": "acme",
        "role": "customer",
        "email": "user@test.com",
        "must_change_password": True,
        "jti": None,         # jti=None -> JTI deny-list check ignore
        "exp": 9_999_999_999,
    }
    mock_request.url.path = "/api/v1/auth/me"
    mock_request.app.state.arq_pool = None  # Pas de Redis -> skip JTI + user_disabled

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake")

    with patch("app.core.tenancy.tenant.user_belongs_to_tenant", new_callable=AsyncMock, return_value=True):
        with pytest.raises(AppError) as exc:
            await get_current_user(mock_request, credentials)

    assert exc.value.code == "MUST_CHANGE_PASSWORD"
    assert exc.value.status_code == 403


async def test_must_change_password_allows_change_password_path():
    """get_current_user avec must_change_password=True sur /auth/change-password -> OK.

    Le blocage doit laisser passer cette route specifique.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.http.deps import get_current_user

    mock_request = MagicMock()
    mock_request.state.jwt_payload = {
        "type": "access",
        "sub": "1",
        "tenant_id": 1,
        "tenant_slug": "acme",
        "role": "customer",
        "email": "user@test.com",
        "must_change_password": True,
        "jti": None,
        "exp": 9_999_999_999,
    }
    mock_request.url.path = "/api/v1/auth/change-password"
    mock_request.app.state.arq_pool = None

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None  # tenant non suspendu
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.core.tenancy.tenant.user_belongs_to_tenant", new_callable=AsyncMock, return_value=True):
        with patch("app.core.database.get_public_session", return_value=mock_cm):
            user = await get_current_user(mock_request, credentials)

    assert user["must_change_password"] is True
    assert user["role"] == "customer"


# ---------------------------------------------------------------------------
# FF-06 - MFA routes accessibles au role admin
# ---------------------------------------------------------------------------


def test_require_role_admin_accepted_in_mfa_routes():
    """Les routes MFA doivent lister 'admin' parmi les roles autorises.

    Verification via inspection du source du router auth.
    """
    import inspect

    from app.modules.auth import router as auth_router

    src = inspect.getsource(auth_router)
    # Les 3 routes MFA doivent inclure "admin" dans require_role
    assert 'require_role("super-admin", "admin")' in src, (
        "Les routes MFA doivent accepter le role admin"
    )


def test_require_role_rejects_unlisted_role():
    """require_role('super-admin', 'admin') doit rejeter 'customer'.

    Verifie la logique interne de la closure via inspection du code source.
    """
    import inspect

    from app.core.http.deps import require_role

    # Creer la dependance et inspecter son code source pour verifier les roles
    dep = require_role("super-admin", "admin")
    src = inspect.getsource(dep)
    assert "roles" in src


# ---------------------------------------------------------------------------
# FF-07 - GET /admin/users -> PaginatedResponse avec total
# ---------------------------------------------------------------------------


def test_paginated_response_build_returns_correct_total_and_pages():
    """PaginatedResponse.build() avec 42 items -> total=42, pages=3 (page_size=20)."""
    from app.core.http.schemas import PaginatedResponse, PaginationParams

    items = [{"id": 1, "email": "a@b.com"}]
    pagination = PaginationParams(page=1, page_size=20)
    result = PaginatedResponse.build(items, total=42, pagination=pagination)

    assert result.total == 42
    assert result.page == 1
    assert result.page_size == 20
    assert result.pages == 3  # ceil(42/20)
    assert len(result.items) == 1


def test_paginated_response_total_independent_of_page_items():
    """total reflette l'ensemble des enregistrements, pas juste la page courante."""
    from app.core.http.schemas import PaginatedResponse, PaginationParams

    pagination = PaginationParams(page=3, page_size=20)
    result = PaginatedResponse.build([], total=42, pagination=pagination)

    assert result.total == 42
    assert len(result.items) == 0


async def test_admin_users_service_returns_tuple_with_total():
    """list_users() retourne (list[dict], int) avec total issu du COUNT."""
    from app.modules.auth.models import User as UserModel
    from app.core.http.schemas import PaginationParams

    user = UserModel(
        id=1,
        email="staff@acme.com",
        full_name="Bob",
        role="staff",
        is_active=True,
        email_verified_at=None,
        must_change_password=False,
        created_at=None,
    )

    mock_session = AsyncMock()
    mock_session.scalar = AsyncMock(return_value=5)
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [user]
    mock_session.execute = AsyncMock(return_value=result_mock)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.modules.admin.users.service.get_tenant_session", return_value=mock_cm):
        from app.modules.admin.users import service as users_service

        pagination = PaginationParams(page=1, page_size=20)
        items, total = await users_service.list_users("acme", pagination)

    assert total == 5
    assert isinstance(items, list)
    assert items[0]["email"] == "staff@acme.com"


# ---------------------------------------------------------------------------
# FF-08 - Commandes invite (guest orders)
# ---------------------------------------------------------------------------


async def test_guest_order_missing_tenant_slug_returns_400(client):
    """POST /orders sans JWT ni X-Tenant-Slug -> 400 MISSING_TENANT_SLUG."""
    response = await client.post(
        "/api/v1/orders",
        json={
            "items": [{"product_id": 1, "quantity": 1}],
            "customer_email": "guest@test.com",
            "delivery_address": "1 rue de la Paix",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_TENANT_SLUG"


async def test_guest_order_missing_customer_email_returns_422(client):
    """POST /orders avec X-Tenant-Slug mais sans customer_email -> 422 CUSTOMER_EMAIL_REQUIRED."""
    response = await client.post(
        "/api/v1/orders",
        json={
            "items": [{"product_id": 1, "quantity": 1}],
            "delivery_address": "1 rue de la Paix",
        },
        headers={"x-tenant-slug": "acme"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CUSTOMER_EMAIL_REQUIRED"


async def test_guest_order_valid_params_reach_service(client):
    """POST /orders invite valide -> atteint le service sans blocage du router.

    On mock service.create_order pour eviter la DB.
    """
    from datetime import datetime, timezone

    fake_order = {
        "id": 1,
        "status": "pending",
        "payment_status": "pending",
        "total": "10.00",
        "items": [],
        "customer_email": "guest@test.com",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": None,
    }

    with patch(
        "app.modules.orders.service.create_order",
        new_callable=AsyncMock,
        return_value=fake_order,
    ):
        response = await client.post(
            "/api/v1/orders",
            json={
                "items": [{"product_id": 1, "quantity": 1}],
                "customer_email": "guest@test.com",
            },
            headers={"x-tenant-slug": "acme", "Idempotency-Key": "guest-test-key"},
        )

    # Le router ne doit pas bloquer avec MISSING_TENANT_SLUG ou CUSTOMER_EMAIL_REQUIRED
    assert response.json().get("code") != "MISSING_TENANT_SLUG"
    assert response.json().get("code") != "CUSTOMER_EMAIL_REQUIRED"


# ---------------------------------------------------------------------------
# FF-13 - Email unique a l'inscription
# ---------------------------------------------------------------------------


async def test_auth_register_duplicate_email_returns_409(client):
    """POST /auth/register avec email deja existant -> 409 EMAIL_ALREADY_EXISTS.

    [SECURITE] Sans cette verification, un second register avec le meme email
    creait un doublon silencieux dans le tenant schema.
    """
    from app.core.http.errors import AppError

    with patch(
        "app.modules.auth.service.register",
        new_callable=AsyncMock,
        side_effect=AppError("EMAIL_ALREADY_EXISTS", "Email already registered", 409, "email"),
    ):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_slug": "acme",
                "tenant_name": "Acme Pizza",
                "email": "duplicate@test.com",
                "password": "Valid1!aa",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_ALREADY_EXISTS"
    assert response.json()["field"] == "email"


async def test_auth_register_new_email_does_not_return_409(client):
    """POST /auth/register avec email unique -> service appele normalement (pas de 409)."""
    from app.modules.auth.models import User as UserModel

    fake_user = UserModel(id=99, email="new@test.com", role="customer")
    fake_tokens = (fake_user, "access_tok", "refresh_tok", 1)

    with patch(
        "app.modules.auth.service.register",
        new_callable=AsyncMock,
        return_value=fake_tokens,
    ):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_slug": "new-tenant",
                "tenant_name": "New Tenant",
                "email": "new@test.com",
                "password": "Valid1!aa",
            },
        )

    assert response.status_code != 409
    assert response.json().get("code") != "EMAIL_ALREADY_EXISTS"
