"""Tests HTTP pour app.modules.pos.router.

Le service layer (echange de token, persistance, state Redis) est deja
teste unitairement dans tests/test_pos_connect_service.py -- ces tests
verifient uniquement le cablage des routes : auth/role, codes de statut,
forme des reponses, redirections du callback. Suit le style de
tests/test_stock_phase2.py (dependency_overrides[get_current_user]) et de
tests/test_catalog_provider.py (app.state.arq_pool manuel, pas de lifespan
dans le fixture `client`).
"""
import pytest
from unittest.mock import AsyncMock

from app.core.http.deps import get_current_user
from app.core.http.errors import AppError
from app.main import app
from app.modules.pos import router as pos_router


@pytest.fixture
def admin_user_override():
    async def _admin() -> dict:
        return {"id": "1", "tenant_id": 1, "tenant_slug": "acme", "role": "admin", "email": "a@acme.test"}

    app.dependency_overrides[get_current_user] = _admin
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def staff_user_override():
    async def _staff() -> dict:
        return {"id": "2", "tenant_id": 1, "tenant_slug": "acme", "role": "staff", "email": "s@acme.test"}

    app.dependency_overrides[get_current_user] = _staff
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _arq_pool():
    had = hasattr(app.state, "arq_pool")
    previous = getattr(app.state, "arq_pool", None)
    app.state.arq_pool = AsyncMock()
    yield
    if had:
        app.state.arq_pool = previous
    else:
        del app.state.arq_pool


# ---------------------------------------------------------------------------
# POST /start
# ---------------------------------------------------------------------------


async def test_start_requires_authentication(client):
    response = await client.post("/api/v1/pos/connect/start")
    assert response.status_code == 401


async def test_start_rejects_non_admin_role(client, staff_user_override):
    response = await client.post("/api/v1/pos/connect/start")
    assert response.status_code == 403


async def test_start_returns_authorization_url(client, admin_user_override, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "get_active_connection", AsyncMock(return_value=None))
    monkeypatch.setattr(pos_router.pos_service, "generate_state", lambda: "fixed-state")
    monkeypatch.setattr(pos_router.pos_service, "store_oauth_state", AsyncMock())
    monkeypatch.setattr(
        pos_router.pos_service,
        "build_authorization_url",
        lambda state: f"https://hub.example/authorize?state={state}",
    )

    response = await client.post("/api/v1/pos/connect/start")

    assert response.status_code == 200
    assert response.json() == {"url": "https://hub.example/authorize?state=fixed-state"}


async def test_start_rejects_when_already_connected(client, admin_user_override, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "get_active_connection", AsyncMock(return_value={"id": 1}))

    response = await client.post("/api/v1/pos/connect/start")

    assert response.status_code == 409
    assert response.json()["code"] == "POS_ALREADY_CONNECTED"


# ---------------------------------------------------------------------------
# GET /callback
# ---------------------------------------------------------------------------


async def test_callback_redirects_with_error_reason_on_missing_code(client):
    response = await client.get(
        "/api/v1/pos/connect/callback", params={"state": "s1"}, follow_redirects=False
    )
    assert response.status_code in (302, 307)
    assert "status=error" in response.headers["location"]
    assert "reason=denied" in response.headers["location"]


async def test_callback_redirects_with_error_reason_on_provider_error_param(client):
    response = await client.get(
        "/api/v1/pos/connect/callback",
        params={"state": "s1", "error": "access_denied"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "reason=denied" in response.headers["location"]


async def test_callback_redirects_on_invalid_state(client, monkeypatch):
    monkeypatch.setattr(
        pos_router.pos_service,
        "consume_oauth_state",
        AsyncMock(side_effect=AppError("POS_OAUTH_INVALID_STATE", "invalid", 400)),
    )
    response = await client.get(
        "/api/v1/pos/connect/callback", params={"code": "c1", "state": "bad"}, follow_redirects=False
    )
    assert response.status_code in (302, 307)
    assert "reason=invalid_state" in response.headers["location"]


async def test_callback_redirects_on_exchange_failure(client, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "consume_oauth_state", AsyncMock(return_value="acme"))
    monkeypatch.setattr(
        pos_router.pos_service,
        "exchange_code_for_tokens",
        AsyncMock(side_effect=AppError("POS_OAUTH_EXCHANGE_FAILED", "failed", 502)),
    )
    response = await client.get(
        "/api/v1/pos/connect/callback", params={"code": "c1", "state": "s1"}, follow_redirects=False
    )
    assert response.status_code in (302, 307)
    assert "reason=exchange_failed" in response.headers["location"]


async def test_callback_redirects_to_success_on_valid_flow(client, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "consume_oauth_state", AsyncMock(return_value="acme"))
    monkeypatch.setattr(
        pos_router.pos_service,
        "exchange_code_for_tokens",
        AsyncMock(return_value={"access_token": "t", "external_establishment_id": "e1"}),
    )
    save_connection_mock = AsyncMock()
    monkeypatch.setattr(pos_router.pos_service, "save_connection", save_connection_mock)

    response = await client.get(
        "/api/v1/pos/connect/callback", params={"code": "c1", "state": "s1"}, follow_redirects=False
    )

    assert response.status_code in (302, 307)
    assert "status=success" in response.headers["location"]
    save_connection_mock.assert_awaited_once_with(
        "acme", {"access_token": "t", "external_establishment_id": "e1"}
    )


async def test_callback_response_never_contains_access_token(client, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "consume_oauth_state", AsyncMock(return_value="acme"))
    monkeypatch.setattr(
        pos_router.pos_service,
        "exchange_code_for_tokens",
        AsyncMock(return_value={"access_token": "super-secret-token", "external_establishment_id": "e1"}),
    )
    monkeypatch.setattr(pos_router.pos_service, "save_connection", AsyncMock())

    response = await client.get(
        "/api/v1/pos/connect/callback", params={"code": "c1", "state": "s1"}, follow_redirects=False
    )

    assert "super-secret-token" not in response.text
    assert "super-secret-token" not in response.headers.get("location", "")


# ---------------------------------------------------------------------------
# POST /disconnect
# ---------------------------------------------------------------------------


async def test_disconnect_requires_authentication(client):
    response = await client.post("/api/v1/pos/connect/disconnect")
    assert response.status_code == 401


async def test_disconnect_rejects_non_admin_role(client, staff_user_override):
    response = await client.post("/api/v1/pos/connect/disconnect")
    assert response.status_code == 403


async def test_disconnect_returns_revoked_status(client, admin_user_override, monkeypatch):
    monkeypatch.setattr(pos_router.pos_service, "disconnect", AsyncMock())
    response = await client.post("/api/v1/pos/connect/disconnect")
    assert response.status_code == 200
    assert response.json() == {"status": "revoked"}


async def test_disconnect_returns_404_when_not_connected(client, admin_user_override, monkeypatch):
    monkeypatch.setattr(
        pos_router.pos_service,
        "disconnect",
        AsyncMock(side_effect=AppError("POS_NOT_CONNECTED", "none", 404)),
    )
    response = await client.post("/api/v1/pos/connect/disconnect")
    assert response.status_code == 404
