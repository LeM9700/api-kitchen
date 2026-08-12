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
