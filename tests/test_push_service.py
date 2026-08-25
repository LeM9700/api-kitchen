"""Tests — envoi push effectif (app/modules/notifications/push_service.py).

La partie WS (auth, tenant, rate limiting) est deja couverte par
tests/test_notifications_ws_security.py -- pas duplique ici. Ce fichier
couvre uniquement l'envoi APNs/FCM : signature JWT ES256, cache/renouvellement
du token APNs, OAuth2 service-account FCM, dispatch HTTP, et desactivation
d'un token invalide (410 APNs / UNREGISTERED FCM).

Aucun appel reseau reel : httpx.AsyncClient est monkeypatche au point d'import
(meme pattern que tests/test_pos_connect_service.py::_FakeAsyncClient), et la
cle privee APNs utilisee pour signer est generee a la volee (EC P-256
jetable, jamais persistee) -- pas de secret dans le repo.
"""
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import settings
from app.modules.notifications import push_service
from app.modules.notifications.models import DeviceToken


# ---------------------------------------------------------------------------
# Cle EC P-256 jetable pour signer/verifier les JWT APNs de test
# ---------------------------------------------------------------------------


def _generate_test_ec_keypair() -> tuple[str, str]:
    """Genere une paire de cles EC P-256 jetable (PEM). Jamais persistee."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


_TEST_PRIVATE_PEM, _TEST_PUBLIC_PEM = _generate_test_ec_keypair()


def _device_token(**overrides) -> DeviceToken:
    defaults = dict(
        id=1,
        user_id=42,
        platform="ios",
        token="raw-device-token",
        is_active=True,
        last_used_at=None,
    )
    defaults.update(overrides)
    return DeviceToken(**defaults)


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response, *, capture: dict | None = None):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["json"] = json
            self._capture["headers"] = headers
        return self._response


def _patch_httpx(monkeypatch, response, capture: dict | None = None):
    monkeypatch.setattr(
        push_service.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(response, capture=capture)
    )


def _fail_if_httpx_called(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("httpx.AsyncClient should not be called")

    monkeypatch.setattr(push_service.httpx, "AsyncClient", _boom)


@pytest.fixture(autouse=True)
def _reset_module_caches(monkeypatch):
    """Les tokens APNs/FCM sont mis en cache dans des globals module-level --
    on repart d'un etat propre a chaque test pour eviter la pollution croisee."""
    monkeypatch.setattr(push_service, "_apns_token", None)
    monkeypatch.setattr(push_service, "_apns_token_issued_at", 0.0)
    monkeypatch.setattr(push_service, "_fcm_credentials", None)


# ---------------------------------------------------------------------------
# _load_apns_private_key
# ---------------------------------------------------------------------------


def test_load_apns_private_key_normalizes_literal_newlines(monkeypatch):
    monkeypatch.setattr(settings, "apns_private_key_path", None)
    monkeypatch.setattr(settings, "apns_private_key", _TEST_PRIVATE_PEM.replace("\n", "\\n"))

    key = push_service._load_apns_private_key()

    assert key == _TEST_PRIVATE_PEM


def test_load_apns_private_key_reads_file_when_path_set(monkeypatch, tmp_path):
    key_file = tmp_path / "apns_key.p8"
    key_file.write_text(_TEST_PRIVATE_PEM)
    monkeypatch.setattr(settings, "apns_private_key_path", str(key_file))
    monkeypatch.setattr(settings, "apns_private_key", None)

    key = push_service._load_apns_private_key()

    assert key == _TEST_PRIVATE_PEM


def test_load_apns_private_key_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "apns_private_key_path", None)
    monkeypatch.setattr(settings, "apns_private_key", None)

    with pytest.raises(RuntimeError):
        push_service._load_apns_private_key()


# ---------------------------------------------------------------------------
# _build_apns_token / _get_apns_token
# ---------------------------------------------------------------------------


def test_build_apns_token_raises_when_key_id_or_team_id_missing(monkeypatch):
    monkeypatch.setattr(settings, "apns_key_id", None)
    monkeypatch.setattr(settings, "apns_team_id", "TEAM123")

    with pytest.raises(RuntimeError):
        push_service._build_apns_token()


def test_build_apns_token_produces_valid_es256_jwt_with_correct_claims(monkeypatch):
    monkeypatch.setattr(settings, "apns_key_id", "KEYID123")
    monkeypatch.setattr(settings, "apns_team_id", "TEAM456")
    monkeypatch.setattr(settings, "apns_private_key_path", None)
    monkeypatch.setattr(settings, "apns_private_key", _TEST_PRIVATE_PEM)

    token = push_service._build_apns_token()

    header = jwt.get_unverified_header(token)
    assert header["kid"] == "KEYID123"
    assert header["alg"] == "ES256"

    # Verifie la signature ES256 avec la cle PUBLIQUE correspondante --
    # confirme que la cle privee configuree a bien ete utilisee pour signer.
    claims = jwt.decode(token, _TEST_PUBLIC_PEM, algorithms=["ES256"])
    assert claims["iss"] == "TEAM456"
    assert abs(claims["iat"] - int(time.time())) < 5


def test_get_apns_token_caches_within_ttl(monkeypatch):
    build_calls = []

    def _fake_build():
        build_calls.append(1)
        return f"token-{len(build_calls)}"

    monkeypatch.setattr(push_service, "_build_apns_token", _fake_build)

    first = push_service._get_apns_token()
    second = push_service._get_apns_token()

    assert first == second == "token-1"
    assert len(build_calls) == 1


def test_get_apns_token_renews_after_ttl_expires(monkeypatch):
    build_calls = []

    def _fake_build():
        build_calls.append(1)
        return f"token-{len(build_calls)}"

    monkeypatch.setattr(push_service, "_build_apns_token", _fake_build)

    first = push_service._get_apns_token()
    # Simule l'expiration : issued_at recule au-dela du TTL (55 min).
    monkeypatch.setattr(
        push_service, "_apns_token_issued_at", time.time() - push_service._APNS_TOKEN_TTL - 1
    )
    second = push_service._get_apns_token()

    assert first == "token-1"
    assert second == "token-2"
    assert len(build_calls) == 2


# ---------------------------------------------------------------------------
# _get_fcm_access_token
# ---------------------------------------------------------------------------


def test_get_fcm_access_token_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", None)
    monkeypatch.setattr(settings, "fcm_service_account_json", None)

    with pytest.raises(RuntimeError):
        push_service._get_fcm_access_token()


def test_get_fcm_access_token_builds_and_refreshes_when_expired(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(
        settings, "fcm_service_account_json", json.dumps({"type": "service_account"})
    )

    fake_creds = MagicMock(valid=False, expired=True, token="fresh-oauth-token")

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ) as from_info:
        token = push_service._get_fcm_access_token()

    from_info.assert_called_once()
    call_kwargs = from_info.call_args.kwargs
    assert call_kwargs["scopes"] == ["https://www.googleapis.com/auth/firebase.messaging"]
    fake_creds.refresh.assert_called_once()
    assert token == "fresh-oauth-token"


def test_get_fcm_access_token_reuses_valid_cached_credentials(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(
        settings, "fcm_service_account_json", json.dumps({"type": "service_account"})
    )
    fake_creds = MagicMock(valid=True, expired=False, token="cached-oauth-token")
    monkeypatch.setattr(push_service, "_fcm_credentials", fake_creds)

    token = push_service._get_fcm_access_token()

    fake_creds.refresh.assert_not_called()
    assert token == "cached-oauth-token"


# ---------------------------------------------------------------------------
# _send_apns
# ---------------------------------------------------------------------------


async def test_send_apns_skips_when_bundle_id_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "apns_bundle_id", None)
    _fail_if_httpx_called(monkeypatch)
    session = AsyncMock()

    result = await push_service._send_apns(session, _device_token(), "Titre", "Corps", {})

    assert result is False
    session.commit.assert_not_called()


async def test_send_apns_success_updates_last_used_and_commits(monkeypatch):
    monkeypatch.setattr(settings, "apns_bundle_id", "com.acme.app")
    monkeypatch.setattr(push_service, "_get_apns_token", lambda: "fake-apns-jwt")
    session = AsyncMock()
    device = _device_token()
    capture: dict = {}
    _patch_httpx(monkeypatch, _FakeResponse(200), capture=capture)

    result = await push_service._send_apns(session, device, "Commande prete", "Venez recuperer", {"order_id": "7"})

    assert result is True
    assert device.last_used_at is not None
    session.commit.assert_called_once()
    assert capture["headers"]["apns-topic"] == "com.acme.app"
    assert capture["headers"]["authorization"] == "bearer fake-apns-jwt"
    assert capture["json"]["aps"]["alert"] == {"title": "Commande prete", "body": "Venez recuperer"}
    assert capture["json"]["order_id"] == "7"


async def test_send_apns_410_deactivates_token(monkeypatch):
    monkeypatch.setattr(settings, "apns_bundle_id", "com.acme.app")
    monkeypatch.setattr(push_service, "_get_apns_token", lambda: "fake-apns-jwt")
    session = AsyncMock()
    device = _device_token(is_active=True)
    _patch_httpx(monkeypatch, _FakeResponse(410))

    result = await push_service._send_apns(session, device, "T", "B", {})

    assert result is False
    assert device.is_active is False
    session.commit.assert_called_once()


async def test_send_apns_other_http_error_returns_false_without_deactivating(monkeypatch):
    monkeypatch.setattr(settings, "apns_bundle_id", "com.acme.app")
    monkeypatch.setattr(push_service, "_get_apns_token", lambda: "fake-apns-jwt")
    session = AsyncMock()
    device = _device_token(is_active=True)
    _patch_httpx(monkeypatch, _FakeResponse(500, text="internal server error"))

    result = await push_service._send_apns(session, device, "T", "B", {})

    assert result is False
    assert device.is_active is True
    session.commit.assert_not_called()


async def test_send_apns_network_exception_is_caught(monkeypatch):
    monkeypatch.setattr(settings, "apns_bundle_id", "com.acme.app")
    monkeypatch.setattr(push_service, "_get_apns_token", lambda: "fake-apns-jwt")
    session = AsyncMock()

    def _raise(*a, **kw):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(push_service.httpx, "AsyncClient", _raise)

    result = await push_service._send_apns(session, _device_token(), "T", "B", {})

    assert result is False


@pytest.mark.parametrize(
    "environment,expected_host",
    [
        ("local", "api.sandbox.push.apple.com"),
        ("production", "api.push.apple.com"),
    ],
)
async def test_send_apns_uses_sandbox_outside_production(monkeypatch, environment, expected_host):
    monkeypatch.setattr(settings, "apns_bundle_id", "com.acme.app")
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(push_service, "_get_apns_token", lambda: "fake-apns-jwt")
    session = AsyncMock()
    capture: dict = {}
    _patch_httpx(monkeypatch, _FakeResponse(200), capture=capture)

    await push_service._send_apns(session, _device_token(), "T", "B", {})

    assert expected_host in capture["url"]


# ---------------------------------------------------------------------------
# _send_fcm
# ---------------------------------------------------------------------------


async def test_send_fcm_skips_when_project_id_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", None)
    _fail_if_httpx_called(monkeypatch)
    session = AsyncMock()

    result = await push_service._send_fcm(session, _device_token(platform="android"), "T", "B", {})

    assert result is False
    session.commit.assert_not_called()


async def test_send_fcm_success_updates_last_used_and_stringifies_data(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(push_service, "_get_fcm_access_token", lambda: "fake-fcm-oauth")
    session = AsyncMock()
    device = _device_token(platform="android", token="fcm-token-xyz")
    capture: dict = {}
    _patch_httpx(monkeypatch, _FakeResponse(200), capture=capture)

    result = await push_service._send_fcm(
        session, device, "Commande prete", "Venez recuperer", {"order_id": 7, "urgent": True}
    )

    assert result is True
    assert device.last_used_at is not None
    session.commit.assert_called_once()
    assert capture["headers"]["authorization"] == "Bearer fake-fcm-oauth"
    msg = capture["json"]["message"]
    assert msg["token"] == "fcm-token-xyz"
    assert msg["data"] == {"order_id": "7", "urgent": "True"}  # FCM exige des strings
    assert f"acme-project" in capture["url"]


async def test_send_fcm_unregistered_error_deactivates_token(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(push_service, "_get_fcm_access_token", lambda: "fake-fcm-oauth")
    session = AsyncMock()
    device = _device_token(platform="android", is_active=True)
    error_body = {"error": {"details": [{"errorCode": "UNREGISTERED"}]}}
    _patch_httpx(monkeypatch, _FakeResponse(400, json_data=error_body))

    result = await push_service._send_fcm(session, device, "T", "B", {})

    assert result is False
    assert device.is_active is False
    session.commit.assert_called_once()


async def test_send_fcm_404_deactivates_token_even_without_error_code(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(push_service, "_get_fcm_access_token", lambda: "fake-fcm-oauth")
    session = AsyncMock()
    device = _device_token(platform="android", is_active=True)
    _patch_httpx(monkeypatch, _FakeResponse(404, json_data={}))

    result = await push_service._send_fcm(session, device, "T", "B", {})

    assert result is False
    assert device.is_active is False


async def test_send_fcm_other_error_returns_false_without_deactivating(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(push_service, "_get_fcm_access_token", lambda: "fake-fcm-oauth")
    session = AsyncMock()
    device = _device_token(platform="android", is_active=True)
    _patch_httpx(monkeypatch, _FakeResponse(500, json_data={}, text="server error"))

    result = await push_service._send_fcm(session, device, "T", "B", {})

    assert result is False
    assert device.is_active is True
    session.commit.assert_not_called()


async def test_send_fcm_network_exception_is_caught(monkeypatch):
    monkeypatch.setattr(settings, "fcm_project_id", "acme-project")
    monkeypatch.setattr(push_service, "_get_fcm_access_token", lambda: "fake-fcm-oauth")
    session = AsyncMock()

    def _raise(*a, **kw):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(push_service.httpx, "AsyncClient", _raise)

    result = await push_service._send_fcm(session, _device_token(platform="android"), "T", "B", {})

    assert result is False


# ---------------------------------------------------------------------------
# send_push_notification (orchestration)
# ---------------------------------------------------------------------------


async def test_send_push_notification_no_active_tokens_returns_zero():
    session = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = []
    session.execute.return_value = result_proxy

    result = await push_service.send_push_notification(
        session, "acme", user_id=42, title="T", body="B", data={}
    )

    assert result == {"sent": 0, "failed": 0}


async def test_send_push_notification_dispatches_per_platform(monkeypatch):
    ios_token = _device_token(id=1, platform="ios")
    android_token = _device_token(id=2, platform="android")
    session = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = [ios_token, android_token]
    session.execute.return_value = result_proxy

    with (
        patch.object(push_service, "_send_apns", new=AsyncMock(return_value=True)) as apns,
        patch.object(push_service, "_send_fcm", new=AsyncMock(return_value=False)) as fcm,
    ):
        result = await push_service.send_push_notification(
            session, "acme", user_id=42, title="T", body="B", data={}
        )

    apns.assert_awaited_once_with(session, ios_token, "T", "B", {})
    fcm.assert_awaited_once_with(session, android_token, "T", "B", {})
    assert result == {"sent": 1, "failed": 1}


async def test_send_push_notification_isolates_per_token_failure(monkeypatch):
    """Une exception inattendue sur UN token ne doit pas empecher l'envoi aux autres."""
    broken_token = _device_token(id=1, platform="ios")
    healthy_token = _device_token(id=2, platform="android")
    session = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = [broken_token, healthy_token]
    session.execute.return_value = result_proxy

    with (
        patch.object(push_service, "_send_apns", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(push_service, "_send_fcm", new=AsyncMock(return_value=True)) as fcm,
    ):
        result = await push_service.send_push_notification(
            session, "acme", user_id=42, title="T", body="B", data={}
        )

    fcm.assert_awaited_once()
    assert result == {"sent": 1, "failed": 1}


async def test_send_push_notification_unknown_platform_counts_as_failed():
    weird_token = _device_token(id=1, platform="windows-phone")
    session = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = [weird_token]
    session.execute.return_value = result_proxy

    result = await push_service.send_push_notification(
        session, "acme", user_id=42, title="T", body="B", data={}
    )

    assert result == {"sent": 0, "failed": 1}
