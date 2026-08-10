"""Tests pour app.modules.pos.service -- flux OAuth de connexion POS.

Suit le style de tests/test_token_revocation.py : AsyncMock pour Redis, pas
de connexion reelle. Les tests de persistance (get_public_session) et
d'echange HTTP arrivent dans les taches suivantes.
"""
import pytest
from unittest.mock import AsyncMock

from app.core.http.errors import AppError
from app.modules.pos import service


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.getdel = AsyncMock(return_value=None)
    return redis


def test_generate_state_is_url_safe_and_long_enough():
    state = service.generate_state()
    assert len(state) >= 32
    assert all(c.isalnum() or c in "-_" for c in state)


def test_generate_state_is_unique_across_calls():
    assert service.generate_state() != service.generate_state()


async def test_store_oauth_state_sets_key_with_ttl(mock_redis):
    await service.store_oauth_state(mock_redis, "abc123", "acme")
    mock_redis.setex.assert_called_once_with(
        "pos_oauth_state:abc123", service.STATE_TTL_SECONDS, "acme"
    )


async def test_consume_oauth_state_returns_tenant_slug(mock_redis):
    mock_redis.getdel.return_value = "acme"
    result = await service.consume_oauth_state(mock_redis, "abc123")
    assert result == "acme"
    mock_redis.getdel.assert_called_once_with("pos_oauth_state:abc123")


async def test_consume_oauth_state_decodes_bytes(mock_redis):
    mock_redis.getdel.return_value = b"acme"
    assert await service.consume_oauth_state(mock_redis, "abc123") == "acme"


async def test_consume_oauth_state_raises_when_missing_or_expired(mock_redis):
    mock_redis.getdel.return_value = None
    with pytest.raises(AppError) as exc_info:
        await service.consume_oauth_state(mock_redis, "unknown")
    assert exc_info.value.code == "POS_OAUTH_INVALID_STATE"
    assert exc_info.value.status_code == 400


class _StatefulFakeRedis:
    """Double minimal avec un vrai GETDEL (dict), pour prouver l'usage unique
    de bout en bout -- pas seulement que consume_oauth_state lit un mock."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self._store[key] = value

    async def getdel(self, key):
        return self._store.pop(key, None)


async def test_consume_oauth_state_second_consumption_is_rejected_replay():
    redis = _StatefulFakeRedis()
    await service.store_oauth_state(redis, "abc123", "acme")

    first = await service.consume_oauth_state(redis, "abc123")
    assert first == "acme"

    with pytest.raises(AppError) as exc_info:
        await service.consume_oauth_state(redis, "abc123")
    assert exc_info.value.code == "POS_OAUTH_INVALID_STATE"


import contextlib
import json

import httpx
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.services import crypto


# ---------------------------------------------------------------------------
# Test doubles for get_public_session() -- mirrors tests/test_payments.py's
# _FakePublicSession, extended to support scalar/mapping/one() result shapes.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, scalar=None, mapping=None, one=None):
        self._scalar = scalar
        self._mapping = mapping
        self._one = one

    def scalar_one_or_none(self):
        return self._scalar

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def one(self):
        return self._one


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return self._results.pop(0)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_session(monkeypatch, results):
    session = _FakeSession(results)

    @contextlib.asynccontextmanager
    async def fake_get_public_session():
        yield session

    monkeypatch.setattr(service, "get_public_session", fake_get_public_session)
    return session


@pytest.fixture(autouse=True)
def _pos_settings(monkeypatch):
    monkeypatch.setattr(settings, "pos_hub_client_id", "client-123")
    monkeypatch.setattr(settings, "pos_hub_client_secret", "secret-abc")
    monkeypatch.setattr(settings, "pos_hub_authorize_url", "https://hub.example/authorize")
    monkeypatch.setattr(settings, "pos_hub_token_url", "https://hub.example/token")
    monkeypatch.setattr(settings, "pos_hub_revoke_url", "")
    monkeypatch.setattr(settings, "pos_hub_redirect_uri", "https://api.example/pos/connect/callback")
    monkeypatch.setattr(settings, "pos_hub_default_scopes", "orders.read")
    monkeypatch.setattr(settings, "pos_hub_establishment_id_field", "establishment_id")
    monkeypatch.setattr(settings, "pos_hub_provider_name", "generic_hub")
    monkeypatch.setattr(settings, "pos_token_encryption_key", Fernet.generate_key().decode())


# ---------------------------------------------------------------------------
# build_authorization_url
# ---------------------------------------------------------------------------


def test_build_authorization_url_includes_required_params():
    url = service.build_authorization_url("state-xyz")
    assert url.startswith("https://hub.example/authorize?")
    assert "client_id=client-123" in url
    assert "state=state-xyz" in url
    assert "response_type=code" in url
    assert "scope=orders.read" in url


# ---------------------------------------------------------------------------
# exchange_code_for_tokens
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", "https://hub.example/token"), response=self
            )

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def _patch_httpx_post(monkeypatch, response):
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(response))


async def test_exchange_code_for_tokens_returns_parsed_data(monkeypatch):
    _patch_httpx_post(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 3600,
                "establishment_id": "store-9",
                "scope": "orders.read orders.write",
            },
        ),
    )
    result = await service.exchange_code_for_tokens("auth-code")
    assert result == {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_in": 3600,
        "external_establishment_id": "store-9",
        "scope": "orders.read orders.write",
    }


async def test_exchange_code_for_tokens_raises_on_http_error(monkeypatch):
    _patch_httpx_post(monkeypatch, _FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(AppError) as exc_info:
        await service.exchange_code_for_tokens("bad-code")
    assert exc_info.value.code == "POS_OAUTH_EXCHANGE_FAILED"
    assert exc_info.value.status_code == 502


async def test_exchange_code_for_tokens_raises_when_access_token_missing(monkeypatch):
    _patch_httpx_post(monkeypatch, _FakeResponse(200, {"establishment_id": "store-1"}))
    with pytest.raises(AppError) as exc_info:
        await service.exchange_code_for_tokens("code")
    assert exc_info.value.code == "POS_OAUTH_EXCHANGE_FAILED"


async def test_exchange_code_for_tokens_raises_when_establishment_id_missing(monkeypatch):
    _patch_httpx_post(monkeypatch, _FakeResponse(200, {"access_token": "tok"}))
    with pytest.raises(AppError) as exc_info:
        await service.exchange_code_for_tokens("code")
    assert exc_info.value.code == "POS_OAUTH_EXCHANGE_FAILED"


# ---------------------------------------------------------------------------
# get_active_connection
# ---------------------------------------------------------------------------


async def test_get_active_connection_returns_row_when_present(monkeypatch):
    mapping = {"id": 1, "access_token_encrypted": "enc", "refresh_token_encrypted": None}
    _patch_session(monkeypatch, [_Result(mapping=mapping)])
    assert await service.get_active_connection("acme") == mapping


async def test_get_active_connection_returns_none_when_absent(monkeypatch):
    _patch_session(monkeypatch, [_Result(mapping=None)])
    assert await service.get_active_connection("acme") is None


# ---------------------------------------------------------------------------
# save_connection
# ---------------------------------------------------------------------------


async def test_save_connection_encrypts_tokens_and_extracts_establishment_id(monkeypatch):
    session = _patch_session(monkeypatch, [_Result(scalar=42), _Result(), _Result()])
    token_data = {
        "access_token": "plain-access",
        "refresh_token": "plain-refresh",
        "expires_in": 3600,
        "external_establishment_id": "store-1",
        "scope": "orders.read orders.write",
    }

    await service.save_connection("acme", token_data)

    insert_stmt, insert_params = session.executed[1]
    assert "INSERT INTO public.pos_connections" in insert_stmt
    assert insert_params["tenant_id"] == 42
    assert insert_params["provider"] == "generic_hub"
    assert insert_params["external_establishment_id"] == "store-1"
    assert insert_params["access_token_encrypted"] != "plain-access"
    assert crypto.decrypt_secret(insert_params["access_token_encrypted"]) == "plain-access"
    assert crypto.decrypt_secret(insert_params["refresh_token_encrypted"]) == "plain-refresh"
    assert json.loads(insert_params["scopes"]) == ["orders.read", "orders.write"]

    update_stmt, update_params = session.executed[2]
    assert "UPDATE public.tenants" in update_stmt
    assert "integration_mode = 'connected'" in update_stmt
    assert update_params["slug"] == "acme"


async def test_save_connection_raises_when_tenant_not_found(monkeypatch):
    _patch_session(monkeypatch, [_Result(scalar=None)])
    with pytest.raises(AppError) as exc_info:
        await service.save_connection(
            "ghost", {"access_token": "x", "external_establishment_id": "s1"}
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


async def test_disconnect_raises_when_no_active_connection(monkeypatch):
    _patch_session(monkeypatch, [_Result(mapping=None)])
    with pytest.raises(AppError) as exc_info:
        await service.disconnect("acme")
    assert exc_info.value.code == "POS_NOT_CONNECTED"
    assert exc_info.value.status_code == 404


async def test_disconnect_revokes_locally_even_if_provider_call_fails(monkeypatch):
    monkeypatch.setattr(settings, "pos_hub_revoke_url", "https://hub.example/revoke")
    encrypted = crypto.encrypt_secret("plain-token")
    session = _patch_session(
        monkeypatch,
        [
            _Result(mapping={"id": 7, "access_token_encrypted": encrypted, "refresh_token_encrypted": None}),
            _Result(),
            _Result(),
        ],
    )

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(service.httpx, "AsyncClient", lambda *a, **kw: _RaisingClient())

    await service.disconnect("acme")  # ne doit pas lever malgre l'echec de revocation

    update_conn_stmt, _ = session.executed[1]
    assert "status = 'revoked'" in update_conn_stmt
    update_tenant_stmt, update_tenant_params = session.executed[2]
    assert "integration_mode = 'standalone'" in update_tenant_stmt
    assert update_tenant_params["slug"] == "acme"


async def test_disconnect_skips_revoke_call_when_url_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "pos_hub_revoke_url", "")
    encrypted = crypto.encrypt_secret("plain-token")
    _patch_session(
        monkeypatch,
        [
            _Result(mapping={"id": 7, "access_token_encrypted": encrypted, "refresh_token_encrypted": None}),
            _Result(),
            _Result(),
        ],
    )

    def _fail_if_called(*a, **kw):
        raise AssertionError("httpx.AsyncClient should not be called when revoke_url is empty")

    monkeypatch.setattr(service.httpx, "AsyncClient", _fail_if_called)

    await service.disconnect("acme")
