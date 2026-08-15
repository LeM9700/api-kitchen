"""Tests pour la route POST /pos/order-webhook (app/modules/pos/router.py).

Complement route-level a tests/test_order_hub.py (qui couvre la logique
d'application du callback une fois enqueue) : ici, uniquement la surface
HTTP -- configuration, signature, mise en file -- suivant le meme pattern
que tests/test_pos_catalog_webhook.py pour /pos/catalog-webhook.
"""
import hashlib
import hmac


def test_is_order_webhook_configured_false_when_secret_empty(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "")
    assert webhook_service.is_order_webhook_configured() is False


def test_verify_order_signature_accepts_valid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")
    body = b'{"event_id": "evt-1"}'
    signature = hmac.new(b"order-secret", body, hashlib.sha256).hexdigest()

    assert webhook_service.verify_order_signature(body, signature) is True


def test_verify_order_signature_rejects_invalid_hmac(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")
    assert webhook_service.verify_order_signature(b"{}", "wrong-signature") is False


def test_verify_order_signature_rejects_missing_header(monkeypatch):
    from app.core.config import settings
    from app.modules.pos import webhook_service

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")
    assert webhook_service.verify_order_signature(b"{}", None) is False


async def test_order_webhook_returns_503_when_not_configured(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "")
    response = await client.post("/api/v1/pos/order-webhook", json={"event_id": "evt-1"})
    assert response.status_code == 503


async def test_order_webhook_rejects_invalid_signature(monkeypatch, client):
    from app.core.config import settings

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")
    response = await client.post(
        "/api/v1/pos/order-webhook",
        json={"event_id": "evt-1"},
        headers={"X-Hub-Signature": "wrong"},
    )
    assert response.status_code == 401


async def test_order_webhook_rejects_missing_signature(monkeypatch, client):
    from app.core.config import settings

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")
    response = await client.post("/api/v1/pos/order-webhook", json={"event_id": "evt-1"})
    assert response.status_code == 401


async def test_order_webhook_accepts_valid_signature_and_enqueues_raw_body(client, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "pos_hub_order_webhook_secret", "order-secret")

    body = json.dumps(
        {
            "event_id": "evt-1",
            "external_establishment_id": "est-1",
            "status": "accepted",
        }
    ).encode()
    signature = hmac.new(b"order-secret", body, hashlib.sha256).hexdigest()

    fake_redis = AsyncMock()
    app.state.arq_pool = fake_redis
    try:
        response = await client.post(
            "/api/v1/pos/order-webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature": signature},
        )
    finally:
        del app.state.arq_pool

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    # [SECURITE] le corps brut est mis en file tel quel, jamais parse avant --
    # seul process_hub_order_callback (worker) parse le payload.
    fake_redis.enqueue_job.assert_awaited_once_with("process_hub_order_callback", raw_body=body.decode("utf-8"))
