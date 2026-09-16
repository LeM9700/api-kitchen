"""Tests devise tenant — GET/PATCH /tenant/config (currency) + verrou paiements reels."""
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.core.database import get_tenant_session
from app.modules.orders.models import Order
from app.modules.payments.models import Payment


@pytest.fixture(autouse=True)
def _arq_pool_mock():
    """Fournit un ``app.state.arq_pool`` factice -- absent car ``client``/``authed_client``
    ne declenchent jamais le lifespan de l'app (meme pattern que
    tests/test_catalog_override_routes.py, tests/test_pos_connect.py). PATCH
    /tenant/config depend de ``get_arq_pool`` ET l'utilise inconditionnellement
    (invalidation cache statut tenant, voir router.py::patch_config) -- un
    ``AsyncMock`` (pas ``None``) est necessaire pour que ces appels reussissent.

    ``.exists`` doit explicitement retourner une valeur falsy : ce meme pool
    sert aussi de ``redis`` dans ``get_current_user`` (app/core/http/deps.py)
    pour les checks JTI deny-list / user-disabled (``is_jti_revoked``,
    ``is_user_disabled``, tous deux ``bool(await redis.exists(...))``) -- un
    AsyncMock non configure retourne un objet truthy par defaut, ce qui ferait
    rejeter A TORT le token comme revoque (401) sur chaque requete authed_client."""
    from app.main import app

    had_arq_pool = hasattr(app.state, "arq_pool")
    previous_arq_pool = getattr(app.state, "arq_pool", None)
    mock_pool = AsyncMock()
    mock_pool.exists = AsyncMock(return_value=0)
    app.state.arq_pool = mock_pool
    try:
        yield
    finally:
        if had_arq_pool:
            app.state.arq_pool = previous_arq_pool
        else:
            del app.state.arq_pool


@pytest.mark.asyncio
async def test_get_config_includes_currency_default_eur(authed_client: AsyncClient):
    response = await authed_client.get("/api/v1/tenant/config")
    assert response.status_code == 200
    assert response.json()["currency"] == "EUR"


@pytest.mark.asyncio
async def test_patch_config_updates_currency(authed_client: AsyncClient):
    response = await authed_client.patch("/api/v1/tenant/config", json={"currency": "USD"})
    assert response.status_code == 200
    assert response.json()["currency"] == "USD"

    # Persistance confirmee par une lecture separee.
    response = await authed_client.get("/api/v1/tenant/config")
    assert response.json()["currency"] == "USD"


@pytest.mark.asyncio
async def test_patch_config_currency_case_insensitive(authed_client: AsyncClient):
    response = await authed_client.patch("/api/v1/tenant/config", json={"currency": "usd"})
    assert response.status_code == 200
    assert response.json()["currency"] == "USD"


@pytest.mark.asyncio
async def test_patch_config_rejects_unsupported_currency(authed_client: AsyncClient):
    response = await authed_client.patch("/api/v1/tenant/config", json={"currency": "JPY"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_config_currency_is_audited(authed_client: AsyncClient):
    await authed_client.patch("/api/v1/tenant/config", json={"currency": "GBP"})
    response = await authed_client.get("/api/v1/tenant/audit")
    assert response.status_code == 200
    entries = response.json()["items"]
    assert any(entry["field_name"] == "currency" for entry in entries)


@pytest.mark.asyncio
async def test_patch_config_rejects_currency_change_with_real_payment(client: AsyncClient, unique_slug: str):
    """Une fois qu'un paiement reel existe, la devise du tenant est verrouillee.

    Utilise un tenant dedie (pas demo_tenant_slug, partage entre tous les tests
    de ce fichier) pour eviter de polluer les autres tests avec un verrou
    persistant en base.
    """
    email = f"currency-lock-{unique_slug}@test.com"
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "tenant_slug": unique_slug,
            "tenant_name": "Currency Lock Tenant",
            "email": email,
            "password": "Valid1!aa",
        },
    )
    assert register_resp.status_code == 201, register_resp.text
    token = register_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Insertion directe d'une commande + d'un paiement "reel" (statut paid) —
    # aucun endpoint public ne complete un paiement Stripe sans mocker tout le SDK.
    async with get_tenant_session(unique_slug) as session:
        order = Order(total=10.0)
        session.add(order)
        await session.flush()
        session.add(
            Payment(
                order_id=order.id,
                provider="stripe",
                amount=10.0,
                currency="EUR",
                status="paid",
            )
        )
        await session.commit()

    response = await client.patch(
        "/api/v1/tenant/config",
        json={"currency": "USD"},
        headers=headers,
    )
    assert response.status_code == 409

    # La config n'a pas ete modifiee.
    get_resp = await client.get("/api/v1/tenant/config", headers=headers)
    assert get_resp.json()["currency"] == "EUR"
