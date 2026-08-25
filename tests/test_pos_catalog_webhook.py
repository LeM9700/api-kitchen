"""[HubRise] Signature X-HubRise-Hmac-SHA256 signee avec le client_secret
OAuth (pas de secret webhook dedie), routage par connection_id dans l'URL
(le payload HubRise ne porte aucun identifiant de location) -- voir
app/modules/pos/webhook_service.py.
"""
import hashlib
import hmac


def test_is_webhook_configured_false_when_client_secret_empty(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_client_secret", "")
    assert webhook_service.is_webhook_configured() is False


def test_verify_signature_accepts_valid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_client_secret", "test-secret")
    body = b'{"resource_type": "catalog", "event_type": "update"}'
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    assert webhook_service.verify_signature(body, signature) is True


def test_verify_signature_rejects_invalid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_client_secret", "test-secret")
    assert webhook_service.verify_signature(b"{}", "wrong-signature") is False


def test_verify_signature_rejects_missing_header(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_client_secret", "test-secret")
    assert webhook_service.verify_signature(b"{}", None) is False


async def test_is_connection_active_false_when_unknown():
    from app.modules.pos import webhook_service

    assert await webhook_service.is_connection_active(999_999_999) is False


async def test_catalog_webhook_rejects_missing_signature(client):
    response = await client.post(
        "/api/v1/pos/catalog-webhook/1", json={"resource_type": "catalog", "event_type": "update"}
    )
    assert response.status_code in (401, 503)  # 503 si pos_hub_client_secret est vide dans cet environnement


async def test_catalog_webhook_rejects_unknown_connection(monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from app.core.config import settings
    from app.main import app
    from app.modules.pos import webhook_service
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(settings, "pos_hub_client_secret", "test-secret")
    monkeypatch.setattr(webhook_service, "is_connection_active", AsyncMock(return_value=False))

    body = json.dumps({"resource_type": "catalog", "event_type": "update"}).encode()
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/pos/catalog-webhook/424242",
            content=body,
            headers={
                "Content-Type": "application/json",
                webhook_service.WEBHOOK_SIGNATURE_HEADER: signature,
            },
        )

    assert response.status_code == 404


async def test_catalog_webhook_accepts_valid_signature_and_enqueues(client, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from app.core.config import settings
    from app.main import app
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_client_secret", "test-secret")
    monkeypatch.setattr(webhook_service, "is_connection_active", AsyncMock(return_value=True))

    body = json.dumps({"resource_type": "catalog", "event_type": "update"}).encode()
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    arq_pool = AsyncMock()
    app.state.arq_pool = arq_pool
    try:
        response = await client.post(
            "/api/v1/pos/catalog-webhook/777",
            content=body,
            headers={
                "Content-Type": "application/json",
                webhook_service.WEBHOOK_SIGNATURE_HEADER: signature,
            },
        )
    finally:
        del app.state.arq_pool

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    arq_pool.enqueue_job.assert_called_once_with("sync_catalog_from_hub", connection_id=777)
