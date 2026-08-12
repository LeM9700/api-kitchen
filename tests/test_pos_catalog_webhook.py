import hashlib
import hmac


def test_is_webhook_configured_false_when_secret_empty(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_webhook_secret", "")
    assert webhook_service.is_webhook_configured() is False


def test_verify_signature_accepts_valid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_webhook_secret", "test-secret")
    body = b'{"external_establishment_id": "est-1"}'
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    assert webhook_service.verify_signature(body, signature) is True


def test_verify_signature_rejects_invalid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_webhook_secret", "test-secret")
    assert webhook_service.verify_signature(b"{}", "wrong-signature") is False


def test_verify_signature_rejects_missing_header(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_webhook_secret", "test-secret")
    assert webhook_service.verify_signature(b"{}", None) is False


async def test_resolve_connection_id_returns_none_when_unknown():
    from app.modules.pos import webhook_service

    result = await webhook_service.resolve_connection_id("unknown-establishment")
    assert result is None


async def test_catalog_webhook_rejects_missing_signature(client):
    response = await client.post("/api/v1/pos/catalog-webhook", json={"external_establishment_id": "est-1"})
    assert response.status_code in (401, 503)  # 503 if pos_hub_webhook_secret is unset in this environment


async def test_catalog_webhook_accepts_valid_signature_and_enqueues(client, monkeypatch):
    import hashlib
    import hmac
    import json
    from unittest.mock import AsyncMock

    from app.core.config import settings
    from app.main import app
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_webhook_secret", "test-secret")
    monkeypatch.setattr(webhook_service, "resolve_connection_id", AsyncMock(return_value=777))

    body = json.dumps({"external_establishment_id": "est-1"}).encode()
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    app.state.arq_pool = AsyncMock()
    try:
        response = await client.post(
            "/api/v1/pos/catalog-webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature": signature},
        )
    finally:
        del app.state.arq_pool

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
