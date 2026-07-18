from unittest.mock import patch

import pytest
import stripe
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.orders.models import Order
from app.modules.payments import service
from app.modules.payments.models import Payment


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


@pytest.fixture
async def integration_db_session():
    """Session DB reelle (schema tenant 'default') isolee par savepoint.

    Necessite une base Postgres avec les migrations appliquees (schema
    tenant_default). Marque le test comme skip si indisponible en local.
    """
    engine = create_async_engine(settings.test_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text('SET search_path TO "tenant_default", public'))
            session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await conn.rollback()
    except OSError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    finally:
        await engine.dispose()


async def test_webhook_duplicate_event_no_double_finalize(integration_db_session):
    """Deux appels avec le meme event.id Stripe ne finalisent le paiement qu'une fois.

    Regression guard pour l'idempotency par ``processed_webhook_events``
    (contrainte UNIQUE sur ``stripe_event_id``) : simule un rejeu Stripe
    (retry burst) sur le meme event.
    """
    order = Order(user_id=None, status="pending", payment_status="pending", total=12.5)
    integration_db_session.add(order)
    await integration_db_session.flush()

    payment = Payment(
        order_id=order.id,
        provider="stripe",
        provider_payment_id="pi_dup_test_123",
        amount=12.5,
        currency="EUR",
        status="pending",
    )
    integration_db_session.add(payment)
    await integration_db_session.commit()

    event = {
        "id": "evt_dup_test_123",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_dup_test_123"}},
    }

    await service.handle_webhook(integration_db_session, "default", event)
    refreshed = await integration_db_session.get(Payment, payment.id)
    assert refreshed.status == "paid"

    # Rejeu du meme event Stripe (meme evt_id) : doit etre ignore silencieusement.
    with patch.object(service, "finalize_payment") as mock_finalize:
        await service.handle_webhook(integration_db_session, "default", event)
        mock_finalize.assert_not_called()
