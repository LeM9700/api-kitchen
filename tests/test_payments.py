from unittest.mock import patch

import pytest
import stripe

from app.core.http.errors import AppError
from app.modules.payments import service


async def test_create_payment_intent_requires_auth(client):
    response = await client.post("/api/v1/payments/intent", json={"order_id": 1})
    assert response.status_code == 401


async def test_webhook_missing_signature_returns_400(client):
    """Requête webhook sans header stripe-signature → 400.

    [🔒 SÉCURITÉ] Sans signature, n'importe qui peut forger un événement Stripe
    et déclencher des changements de statut de paiement/commande frauduleux.
    """
    response = await client.post(
        "/api/v1/payments/webhook",
        content=b'{"type": "payment_intent.succeeded"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_webhook_invalid_signature_returns_400(client):
    """Requête webhook avec signature HMAC invalide → 400.

    [🔒 SÉCURITÉ] Une signature présente mais incorrecte indique une requête
    forgée ou un secret de webhook compromis/incorrect.
    """
    with patch(
        "stripe.Webhook.construct_event",
        side_effect=stripe.error.SignatureVerificationError(
            "No signatures found matching the expected signature for payload",
            "t=invalid,v1=badsig",
        ),
    ):
        response = await client.post(
            "/api/v1/payments/webhook",
            content=b'{"type": "payment_intent.succeeded"}',
            headers={
                "Content-Type": "application/json",
                "stripe-signature": "t=invalid,v1=badsig",
            },
        )
    assert response.status_code == 400


def test_extract_tenant_slug_from_event_requires_metadata():
    with pytest.raises(AppError) as exc:
        service.extract_tenant_slug_from_event({"data": {"object": {"metadata": {}}}})
    assert exc.value.code == "WEBHOOK_TENANT_REQUIRED"


def test_extract_tenant_slug_from_event_reads_payment_intent_metadata():
    event = {
        "data": {
            "object": {
                "metadata": {
                    "tenant_slug": "pizza-test",
                    "order_id": "1",
                    "payment_id": "2",
                }
            }
        }
    }
    assert service.extract_tenant_slug_from_event(event) == "pizza-test"


def test_local_fallback_disabled_in_production(monkeypatch):
    monkeypatch.setattr(service.settings, "environment", "production")
    assert service._local_fallback_allowed() is False


def test_local_fallback_allowed_in_test(monkeypatch):
    monkeypatch.setattr(service.settings, "environment", "test")
    assert service._local_fallback_allowed() is True


def test_stripe_message_redacts_configured_secrets(monkeypatch):
    monkeypatch.setattr(service.settings, "stripe_secret_key", "sk_live_secret")
    monkeypatch.setattr(service.settings, "stripe_webhook_secret", "whsec_secret")

    class FakeStripeError(Exception):
        user_message = "bad key sk_live_secret and webhook whsec_secret"

    message = service._safe_stripe_message(FakeStripeError())
    assert "sk_live_secret" not in message
    assert "whsec_secret" not in message
    assert "[redacted]" in message


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (12.34, 1234),
        ("10.00", 1000),
        (0, 0),
    ],
)
def test_money_to_cents(amount, expected):
    assert service._money_to_cents(amount) == expected
