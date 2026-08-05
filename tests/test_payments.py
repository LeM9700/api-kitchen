import contextlib
import hashlib
import hmac
import time
from unittest.mock import AsyncMock, patch

import pytest
import stripe
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.orders.models import Order
from app.modules.payments import router as payments_router
from app.modules.payments import service
from app.modules.payments.models import Payment


class _ScalarOneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakePublicSession:
    """Simule get_public_session() pour tester _resolve_tenant_slug_by_stripe_account
    sans base de donnees reelle."""

    def __init__(self, tenant_slug):
        self.tenant_slug = tenant_slug
        self.execute_calls = 0

    async def execute(self, *args, **kwargs):
        self.execute_calls += 1
        return _ScalarOneResult(self.tenant_slug)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_public_session(monkeypatch, tenant_slug):
    fake_session = _FakePublicSession(tenant_slug)

    @contextlib.asynccontextmanager
    async def fake_get_public_session():
        yield fake_session

    monkeypatch.setattr(service, "get_public_session", fake_get_public_session)
    return fake_session


class _FakeRedis:
    """Simule l'interface get/setex utilisee par app.core.services.cache."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


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


def _stripe_signed_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Construit un header ``Stripe-Signature`` valide (memes calculs que WebhookSignature)."""
    timestamp = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
    signature = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


_PLATFORM_SECRET = "whsec_platform_test"
_CONNECT_SECRET = "whsec_connect_test"
_MULTI_SECRETS = [("platform", _PLATFORM_SECRET), ("connect", _CONNECT_SECRET)]


def test_verify_stripe_webhook_event_valid_platform_secret():
    payload = b'{"id": "evt_platform", "object": "event", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = _stripe_signed_header(payload, _PLATFORM_SECRET)

    event = service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)

    assert event["id"] == "evt_platform"


def test_verify_stripe_webhook_event_valid_connect_secret():
    payload = b'{"id": "evt_connect", "object": "event", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = _stripe_signed_header(payload, _CONNECT_SECRET)

    event = service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)

    assert event["id"] == "evt_connect"


def test_verify_stripe_webhook_event_invalid_signature_with_both_secrets_raises():
    payload = b'{"id": "evt_forged", "object": "event", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = _stripe_signed_header(payload, "whsec_attacker_forged")

    with pytest.raises(stripe.error.SignatureVerificationError):
        service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)


def test_verify_stripe_webhook_event_logs_category_never_secret_value(caplog):
    payload = b'{"id": "evt_log", "object": "event", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = _stripe_signed_header(payload, _CONNECT_SECRET)

    with caplog.at_level("INFO", logger="app.modules.payments.service"):
        service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)

    messages = [record.getMessage() for record in caplog.records]
    assert any("connect" in message for message in messages)
    assert all(_CONNECT_SECRET not in message for message in messages)
    assert all(_PLATFORM_SECRET not in message for message in messages)


def test_verify_stripe_webhook_event_failure_is_explicit_and_stable():
    payload = b'{"id": "evt_stable", "object": "event", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = _stripe_signed_header(payload, "whsec_attacker_forged")

    with pytest.raises(stripe.error.SignatureVerificationError) as first:
        service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)
    with pytest.raises(stripe.error.SignatureVerificationError) as second:
        service.verify_stripe_webhook_event(payload, header, secrets=_MULTI_SECRETS)

    assert str(first.value) == str(second.value)
    assert _PLATFORM_SECRET not in str(first.value)
    assert _CONNECT_SECRET not in str(first.value)


async def test_webhook_route_accepts_connect_secret_signature(client, monkeypatch):
    """Le endpoint HTTP /webhook accepte une signature valide avec le secret Connect.

    Isole de la DB reelle (get_tenant_session/extract_tenant_slug_from_event/handle_webhook
    mockes) : verifie uniquement le wiring router -> verify_stripe_webhook_event, pas la
    logique metier deja couverte par test_webhook_duplicate_event_no_double_finalize.
    """
    connect_secret = "whsec_connect_router_test"
    monkeypatch.setattr(service.settings, "stripe_webhook_connect_secret", connect_secret)

    payload = (
        b'{"id": "evt_router_connect", "object": "event", '
        b'"type": "payment_intent.succeeded", "data": {"object": {}}}'
    )
    header = _stripe_signed_header(payload, connect_secret)

    handle_webhook_mock = AsyncMock()
    monkeypatch.setattr(service, "handle_webhook", handle_webhook_mock)
    monkeypatch.setattr(service, "extract_tenant_slug_from_event", AsyncMock(return_value="default"))

    @contextlib.asynccontextmanager
    async def fake_get_tenant_session(tenant_slug):
        yield None

    monkeypatch.setattr(payments_router, "get_tenant_session", fake_get_tenant_session)

    response = await client.post(
        "/api/v1/payments/webhook",
        content=payload,
        headers={"Content-Type": "application/json", "stripe-signature": header},
    )

    assert response.status_code == 204
    handle_webhook_mock.assert_awaited_once()


def test_warn_if_webhook_connect_secret_missing_logs_in_production(monkeypatch, caplog):
    monkeypatch.setattr(service.settings, "environment", "production")
    monkeypatch.setattr(service.settings, "stripe_webhook_connect_secret", "")

    with caplog.at_level("WARNING", logger="app.modules.payments.service"):
        service.warn_if_webhook_connect_secret_missing()

    assert any("STRIPE_WEBHOOK_CONNECT_SECRET" in record.getMessage() for record in caplog.records)


def test_warn_if_webhook_connect_secret_missing_silent_when_configured(monkeypatch, caplog):
    monkeypatch.setattr(service.settings, "environment", "production")
    monkeypatch.setattr(service.settings, "stripe_webhook_connect_secret", "whsec_connect_configured")

    with caplog.at_level("WARNING", logger="app.modules.payments.service"):
        service.warn_if_webhook_connect_secret_missing()

    assert not caplog.records


def test_warn_if_webhook_connect_secret_missing_silent_outside_production(monkeypatch, caplog):
    monkeypatch.setattr(service.settings, "environment", "local")
    monkeypatch.setattr(service.settings, "stripe_webhook_connect_secret", "")

    with caplog.at_level("WARNING", logger="app.modules.payments.service"):
        service.warn_if_webhook_connect_secret_missing()

    assert not caplog.records


async def test_extract_tenant_slug_from_event_requires_metadata():
    with pytest.raises(AppError) as exc:
        await service.extract_tenant_slug_from_event({"data": {"object": {"metadata": {}}}})
    assert exc.value.code == "WEBHOOK_TENANT_REQUIRED"


async def test_extract_tenant_slug_from_event_reads_payment_intent_metadata():
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
    assert await service.extract_tenant_slug_from_event(event) == "pizza-test"


async def test_extract_tenant_slug_from_event_resolves_via_account_field(monkeypatch):
    """En direct charge, charge.dispute.created ne porte aucune metadata propre --
    seul event["account"] permet de router l'event vers le bon tenant."""
    fake_session = _patch_public_session(monkeypatch, "pizza-test")

    event = {
        "type": "charge.dispute.created",
        "account": "acct_123",
        "data": {
            "object": {
                "id": "dp_1",
                "payment_intent": "pi_1",
                "amount": 500,
                "currency": "eur",
                "reason": "fraudulent",
            }
        },
    }
    assert await service.extract_tenant_slug_from_event(event) == "pizza-test"
    assert fake_session.execute_calls == 1


async def test_extract_tenant_slug_from_event_account_field_takes_priority_over_metadata(monkeypatch):
    _patch_public_session(monkeypatch, "account-tenant")

    event = {
        "account": "acct_123",
        "data": {"object": {"metadata": {"tenant_slug": "metadata-tenant"}}},
    }
    assert await service.extract_tenant_slug_from_event(event) == "account-tenant"


async def test_extract_tenant_slug_from_event_caches_account_resolution(monkeypatch):
    fake_session = _patch_public_session(monkeypatch, "pizza-test")
    fake_redis = _FakeRedis()

    event = {"account": "acct_123", "data": {"object": {"metadata": {}}}}
    assert await service.extract_tenant_slug_from_event(event, arq_pool=fake_redis) == "pizza-test"
    assert await service.extract_tenant_slug_from_event(event, arq_pool=fake_redis) == "pizza-test"
    assert fake_session.execute_calls == 1  # 2e appel servi depuis le cache Redis


async def test_extract_tenant_slug_from_event_falls_back_to_retrieve_with_stripe_account(monkeypatch):
    """Sans account resolvable ni metadata, le fallback retrieve doit propager
    stripe_account -- sinon il interroge le compte plateforme et echoue toujours
    pour un PaymentIntent cree en direct charge."""
    _patch_public_session(monkeypatch, None)
    retrieve_calls = {}

    def fake_retrieve(pi_id, **kwargs):
        retrieve_calls["pi_id"] = pi_id
        retrieve_calls["kwargs"] = kwargs
        return {"metadata": {"tenant_slug": "pizza-test"}}

    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", fake_retrieve)

    event = {
        "type": "charge.refunded",
        "account": "acct_123",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_1", "metadata": {}}},
    }
    assert await service.extract_tenant_slug_from_event(event) == "pizza-test"
    assert retrieve_calls["kwargs"] == {"stripe_account": "acct_123"}


async def test_extract_tenant_slug_from_event_logs_error_on_retrieve_failure(monkeypatch, caplog):
    def fake_retrieve(pi_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", fake_retrieve)

    event = {
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_1", "metadata": {}}},
    }
    with caplog.at_level("ERROR", logger="app.modules.payments.service"):
        with pytest.raises(AppError) as exc:
            await service.extract_tenant_slug_from_event(event)
    assert exc.value.code == "WEBHOOK_TENANT_REQUIRED"
    assert any(record.levelname == "ERROR" for record in caplog.records)


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


async def test_create_intent_reuses_active_pending_payment_without_creating_stripe_intent():
    order = Order(id=1, user_id=7, status="pending", payment_status="pending", total=12.5)
    payment = Payment(
        id=2,
        order_id=1,
        provider="stripe",
        provider_payment_id="pi_existing_123",
        amount=12.5,
        currency="EUR",
        status="pending",
        created_by_user_id=7,
    )
    session = AsyncMock()
    session.execute.return_value = _ScalarOneResult(order)
    session.scalar.return_value = payment

    with (
        patch.object(
            service,
            "get_stripe_context",
            new=AsyncMock(return_value=service.StripeContext()),
        ),
        patch(
            "app.modules.payments.service.stripe.PaymentIntent.retrieve",
            return_value={"client_secret": "pi_existing_123_secret_reused"},
        ) as retrieve_intent,
        patch("app.modules.payments.service.stripe.PaymentIntent.create") as create_intent,
    ):
        result = await service.create_intent(
            session,
            1,
            tenant_slug="acme",
            user_id=7,
        )

    assert result["payment"] is payment
    assert result["client_secret"] == "pi_existing_123_secret_reused"
    retrieve_intent.assert_called_once()
    create_intent.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


async def test_create_intent_rejects_already_paid_order():
    order = Order(id=1, user_id=7, status="confirmed", payment_status="paid", total=12.5)
    session = AsyncMock()
    session.execute.return_value = _ScalarOneResult(order)

    with pytest.raises(AppError) as exc:
        await service.create_intent(session, 1, tenant_slug="acme", user_id=7)

    assert exc.value.code == "ORDER_ALREADY_PAID"
    session.scalar.assert_not_called()


async def test_create_intent_rejects_guest_order_for_authenticated_user():
    order = Order(id=1, user_id=None, status="pending", payment_status="pending", total=12.5)
    session = AsyncMock()
    session.execute.return_value = _ScalarOneResult(order)

    with pytest.raises(AppError) as exc:
        await service.create_intent(session, 1, tenant_slug="acme", user_id=7)

    assert exc.value.code == "ORDER_NOT_FOUND"
    session.scalar.assert_not_called()


def _stripe_intent_payload(payment: Payment, order: Order, tenant_slug: str = "acme", **overrides):
    payload = {
        "id": payment.provider_payment_id,
        "status": "succeeded",
        "amount": service._money_to_cents(payment.amount),
        "amount_received": service._money_to_cents(payment.amount),
        "currency": str(payment.currency).lower(),
        "metadata": {
            "tenant_slug": tenant_slug,
            "order_id": str(order.id),
            "payment_id": str(payment.id),
        },
    }
    payload.update(overrides)
    return payload


async def test_confirm_rejects_unowned_payment_before_stripe_verification():
    order = Order(id=1, user_id=8, status="pending", payment_status="pending", total=12.5)
    payment = Payment(
        id=2,
        order_id=1,
        provider="stripe",
        provider_payment_id="pi_owner_test",
        amount=12.5,
        currency="EUR",
        status="pending",
        created_by_user_id=8,
    )
    session = AsyncMock()
    session.scalar.return_value = payment
    session.get.return_value = order

    with (
        patch("app.modules.payments.service.stripe.PaymentIntent.retrieve") as retrieve_intent,
        pytest.raises(AppError) as exc,
    ):
        await service.confirm(session, "pi_owner_test", tenant_slug="acme", user_id=7)

    assert exc.value.code == "PAYMENT_NOT_FOUND"
    retrieve_intent.assert_not_called()
    session.commit.assert_not_called()


async def test_confirm_rejects_unpaid_stripe_intent():
    order = Order(id=1, user_id=7, status="pending", payment_status="pending", total=12.5)
    payment = Payment(
        id=2,
        order_id=1,
        provider="stripe",
        provider_payment_id="pi_unpaid_test",
        amount=12.5,
        currency="EUR",
        status="pending",
        created_by_user_id=7,
    )
    session = AsyncMock()
    session.scalar.return_value = payment
    session.get.return_value = order

    with (
        patch.object(service, "get_stripe_context", new=AsyncMock(return_value=service.StripeContext())),
        patch(
            "app.modules.payments.service.stripe.PaymentIntent.retrieve",
            return_value=_stripe_intent_payload(payment, order, status="requires_payment_method"),
        ),
        pytest.raises(AppError) as exc,
    ):
        await service.confirm(session, "pi_unpaid_test", tenant_slug="acme", user_id=7)

    assert exc.value.code == "PAYMENT_NOT_SUCCEEDED"
    session.commit.assert_not_called()


async def test_confirm_rejects_stripe_metadata_mismatch():
    order = Order(id=1, user_id=7, status="pending", payment_status="pending", total=12.5)
    payment = Payment(
        id=2,
        order_id=1,
        provider="stripe",
        provider_payment_id="pi_metadata_test",
        amount=12.5,
        currency="EUR",
        status="pending",
        created_by_user_id=7,
    )
    session = AsyncMock()
    session.scalar.return_value = payment
    session.get.return_value = order

    with (
        patch.object(service, "get_stripe_context", new=AsyncMock(return_value=service.StripeContext())),
        patch(
            "app.modules.payments.service.stripe.PaymentIntent.retrieve",
            return_value=_stripe_intent_payload(
                payment,
                order,
                metadata={"tenant_slug": "other", "order_id": "1", "payment_id": "2"},
            ),
        ),
        pytest.raises(AppError) as exc,
    ):
        await service.confirm(session, "pi_metadata_test", tenant_slug="acme", user_id=7)

    assert exc.value.code == "PAYMENT_METADATA_MISMATCH"
    session.commit.assert_not_called()


async def test_confirm_finalizes_only_after_verified_stripe_intent():
    order = Order(id=1, user_id=7, status="confirmed", payment_status="pending", total=12.5)
    payment = Payment(
        id=2,
        order_id=1,
        provider="stripe",
        provider_payment_id="pi_success_test",
        amount=12.5,
        currency="EUR",
        status="pending",
        created_by_user_id=7,
    )
    session = AsyncMock()
    session.scalar.return_value = payment
    session.get.side_effect = [order, payment, payment]

    with (
        patch.object(service, "get_stripe_context", new=AsyncMock(return_value=service.StripeContext())),
        patch(
            "app.modules.payments.service.stripe.PaymentIntent.retrieve",
            return_value=_stripe_intent_payload(payment, order),
        ),
        patch.object(service, "_auto_confirm_loyalty_reservation", new=AsyncMock()) as loyalty_confirm,
    ):
        result = await service.confirm(session, "pi_success_test", tenant_slug="acme", user_id=7)

    assert result is payment
    assert payment.status == "paid"
    assert order.payment_status == "paid"
    session.commit.assert_awaited_once()
    loyalty_confirm.assert_awaited_once_with(session, order.id, order.user_id)


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
        "data": {
            "object": {
                "id": "pi_dup_test_123",
                "status": "succeeded",
                "amount": 1250,
                "amount_received": 1250,
                "currency": "eur",
                "metadata": {
                    "tenant_slug": "default",
                    "order_id": str(order.id),
                    "payment_id": str(payment.id),
                },
            }
        },
    }

    await service.handle_webhook(integration_db_session, "default", event)
    refreshed = await integration_db_session.get(Payment, payment.id)
    assert refreshed.status == "paid"

    # Rejeu du meme event Stripe (meme evt_id) : doit etre ignore silencieusement.
    with patch.object(service, "finalize_payment") as mock_finalize:
        await service.handle_webhook(integration_db_session, "default", event)
        mock_finalize.assert_not_called()
