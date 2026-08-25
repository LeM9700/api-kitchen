"""Tests — Stripe Connect (app/modules/payments/connect_router.py + connect_service.py).

Portee : onboarding, statut, lien dashboard Express. Le webhook Stripe (paiements
clients, y compris via compte connecte) est deja couvert par tests/test_payments.py
(test_webhook_route_accepts_connect_secret_signature et voisins) -- pas duplique ici.

Convention de mock reprise de tests/test_payments.py : un faux get_public_session()
(_FakePublicSession) pour exercer le vrai SQL de connect_service._get_stripe_account_id /
_save_stripe_account_id sans base reelle, et patch() direct sur les methodes du SDK
Stripe au point d'import (connect_service.stripe.X).

Aucun appel reseau reel vers Stripe : tout est mocke. C'est du code qui deplace de
l'argent reel (comptes Stripe Connect, virements) -- priorite aux tests d'idempotence
(ne jamais recreer un compte existant) et de propagation d'erreur (jamais avaler une
StripeError silencieusement).
"""
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe

from app.core.auth.security import create_access_token
from app.core.http.errors import AppError
from app.modules.payments import connect_service


# ---------------------------------------------------------------------------
# Fake public session (meme pattern que tests/test_payments.py)
# ---------------------------------------------------------------------------


class _ScalarOneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakePublicSession:
    """Simule get_public_session() pour tester connect_service sans base reelle.

    execute() renvoie toujours stripe_account_id (le seul SELECT que connect_service
    emet est celui de _get_stripe_account_id) ; commit()/execute() suivants sont
    juste comptabilises pour verifier que _save_stripe_account_id a bien ecrit.
    """

    def __init__(self, stripe_account_id: str | None):
        self.stripe_account_id = stripe_account_id
        self.executed: list[tuple[str, dict]] = []
        self.committed = False

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return _ScalarOneResult(self.stripe_account_id)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_public_session(monkeypatch, stripe_account_id: str | None = None):
    fake = _FakePublicSession(stripe_account_id)

    @contextlib.asynccontextmanager
    async def fake_get_public_session():
        yield fake

    monkeypatch.setattr(connect_service, "get_public_session", fake_get_public_session)
    return fake


def _stripe_account(**overrides):
    """MagicMock imitant un objet stripe.Account (acces par attribut)."""
    defaults = {
        "id": "acct_test123",
        "details_submitted": False,
        "payouts_enabled": False,
        "charges_enabled": False,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# ---------------------------------------------------------------------------
# get_or_create_connect_account
# ---------------------------------------------------------------------------


async def test_get_or_create_returns_existing_account_without_stripe_call(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_existing")

    with patch("app.modules.payments.connect_service.stripe.Account.create") as create:
        account_id = await connect_service.get_or_create_connect_account("acme")

    assert account_id == "acct_existing"
    create.assert_not_called()


async def test_get_or_create_creates_new_account_and_persists(monkeypatch):
    fake_session = _patch_public_session(monkeypatch, stripe_account_id=None)

    with patch(
        "app.modules.payments.connect_service.stripe.Account.create",
        return_value=_stripe_account(id="acct_brand_new"),
    ) as create:
        account_id = await connect_service.get_or_create_connect_account(
            "acme", admin_email="owner@acme.test"
        )

    assert account_id == "acct_brand_new"
    create.assert_called_once()
    params = create.call_args.kwargs
    assert params["type"] == "express"
    assert params["country"] == "FR"
    assert params["capabilities"]["card_payments"]["requested"] is True
    assert params["capabilities"]["transfers"]["requested"] is True
    assert params["email"] == "owner@acme.test"
    # persiste bien via UPDATE + commit, pas juste retourne
    assert fake_session.committed is True
    update_calls = [p for _, p in fake_session.executed if p.get("account_id")]
    assert update_calls[0]["account_id"] == "acct_brand_new"
    assert update_calls[0]["slug"] == "acme"


async def test_get_or_create_omits_email_when_not_provided(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id=None)

    with patch(
        "app.modules.payments.connect_service.stripe.Account.create",
        return_value=_stripe_account(),
    ) as create:
        await connect_service.get_or_create_connect_account("acme")

    assert "email" not in create.call_args.kwargs


async def test_get_or_create_wraps_stripe_error(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id=None)

    with (
        patch(
            "app.modules.payments.connect_service.stripe.Account.create",
            side_effect=stripe.error.StripeError("platform account restricted"),
        ),
        pytest.raises(AppError) as exc,
    ):
        await connect_service.get_or_create_connect_account("acme")

    assert exc.value.code == "STRIPE_CONNECT_ERROR"
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# create_account_link
# ---------------------------------------------------------------------------


async def test_create_account_link_returns_url():
    link = MagicMock(url="https://connect.stripe.com/setup/e/acct_test123/abc")

    with patch(
        "app.modules.payments.connect_service.stripe.AccountLink.create",
        return_value=link,
    ) as create_link:
        url = await connect_service.create_account_link(
            "acct_test123",
            return_url="https://app.example.com/return",
            refresh_url="https://app.example.com/refresh",
        )

    assert url == link.url
    create_link.assert_called_once_with(
        account="acct_test123",
        return_url="https://app.example.com/return",
        refresh_url="https://app.example.com/refresh",
        type="account_onboarding",
    )


async def test_create_account_link_wraps_stripe_error():
    with (
        patch(
            "app.modules.payments.connect_service.stripe.AccountLink.create",
            side_effect=stripe.error.StripeError("invalid account"),
        ),
        pytest.raises(AppError) as exc,
    ):
        await connect_service.create_account_link(
            "acct_bad", return_url="https://x/return", refresh_url="https://x/refresh"
        )

    assert exc.value.code == "STRIPE_CONNECT_ERROR"


# ---------------------------------------------------------------------------
# get_connect_status
# ---------------------------------------------------------------------------


async def test_get_connect_status_no_account_skips_stripe_call(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id=None)

    with patch("app.modules.payments.connect_service.stripe.Account.retrieve") as retrieve:
        status = await connect_service.get_connect_status("acme")

    retrieve.assert_not_called()
    assert status == {
        "stripe_account_id": None,
        "details_submitted": False,
        "payouts_enabled": False,
        "charges_enabled": False,
        "onboarding_complete": False,
    }


async def test_get_connect_status_reflects_stripe_flags_when_complete(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")
    account = _stripe_account(
        details_submitted=True, payouts_enabled=True, charges_enabled=True
    )

    with patch(
        "app.modules.payments.connect_service.stripe.Account.retrieve",
        return_value=account,
    ):
        status = await connect_service.get_connect_status("acme")

    assert status["stripe_account_id"] == "acct_test123"
    assert status["onboarding_complete"] is True


async def test_get_connect_status_incomplete_when_charges_not_enabled(monkeypatch):
    """onboarding_complete exige details_submitted ET charges_enabled -- pas l'un sans l'autre."""
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")
    account = _stripe_account(
        details_submitted=True, payouts_enabled=False, charges_enabled=False
    )

    with patch(
        "app.modules.payments.connect_service.stripe.Account.retrieve",
        return_value=account,
    ):
        status = await connect_service.get_connect_status("acme")

    assert status["onboarding_complete"] is False


async def test_get_connect_status_wraps_stripe_error(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")

    with (
        patch(
            "app.modules.payments.connect_service.stripe.Account.retrieve",
            side_effect=stripe.error.StripeError("account not found"),
        ),
        pytest.raises(AppError) as exc,
    ):
        await connect_service.get_connect_status("acme")

    assert exc.value.code == "STRIPE_CONNECT_ERROR"


# ---------------------------------------------------------------------------
# get_dashboard_link
# ---------------------------------------------------------------------------


async def test_get_dashboard_link_raises_not_started_without_account(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id=None)

    with pytest.raises(AppError) as exc:
        await connect_service.get_dashboard_link("acme")

    assert exc.value.code == "STRIPE_CONNECT_NOT_STARTED"
    assert exc.value.status_code == 409


async def test_get_dashboard_link_raises_incomplete_when_onboarding_unfinished(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")
    account = _stripe_account(details_submitted=False)

    with (
        patch(
            "app.modules.payments.connect_service.stripe.Account.retrieve",
            return_value=account,
        ),
        patch(
            "app.modules.payments.connect_service.stripe.Account.create_login_link"
        ) as login_link,
        pytest.raises(AppError) as exc,
    ):
        await connect_service.get_dashboard_link("acme")

    assert exc.value.code == "STRIPE_CONNECT_INCOMPLETE"
    login_link.assert_not_called()  # jamais de lien dashboard tant que l'onboarding n'est pas fini


async def test_get_dashboard_link_returns_login_url_when_complete(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")
    account = _stripe_account(details_submitted=True)
    login_link = MagicMock(url="https://connect.stripe.com/express/acct_test123/xyz")

    with (
        patch(
            "app.modules.payments.connect_service.stripe.Account.retrieve",
            return_value=account,
        ),
        patch(
            "app.modules.payments.connect_service.stripe.Account.create_login_link",
            return_value=login_link,
        ),
    ):
        url = await connect_service.get_dashboard_link("acme")

    assert url == login_link.url


async def test_get_dashboard_link_wraps_stripe_error_from_login_link(monkeypatch):
    _patch_public_session(monkeypatch, stripe_account_id="acct_test123")
    account = _stripe_account(details_submitted=True)

    with (
        patch(
            "app.modules.payments.connect_service.stripe.Account.retrieve",
            return_value=account,
        ),
        patch(
            "app.modules.payments.connect_service.stripe.Account.create_login_link",
            side_effect=stripe.error.StripeError("temporary Stripe outage"),
        ),
        pytest.raises(AppError) as exc,
    ):
        await connect_service.get_dashboard_link("acme")

    assert exc.value.code == "STRIPE_CONNECT_ERROR"


# ---------------------------------------------------------------------------
# Router -- auth et frontieres d'autorisation
# ---------------------------------------------------------------------------


async def _register_admin(client, slug: str) -> dict:
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": slug,
        "tenant_name": f"Pizzeria {slug}",
        "email": f"admin-{slug}@test.com",
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _super_admin_headers() -> dict:
    """Token super-admin (nouveau flux : tenant_slug=None, pas d'appartenance
    tenant a verifier -- voir app/modules/super_admin/router.py)."""
    token = create_access_token({
        "sub": "1",
        "email": "sa@platform.test",
        "role": "super-admin",
        "tenant_slug": None,
        "tenant_id": None,
    })
    return {"Authorization": f"Bearer {token}"}


async def test_onboarding_requires_auth(client):
    resp = await client.post(
        "/api/v1/payments/connect/onboarding",
        json={"return_url": "https://x/return", "refresh_url": "https://x/refresh"},
    )
    assert resp.status_code in (401, 403)


async def test_status_requires_auth(client):
    resp = await client.get("/api/v1/payments/connect/status")
    assert resp.status_code in (401, 403)


async def test_dashboard_requires_auth(client):
    resp = await client.get("/api/v1/payments/connect/dashboard")
    assert resp.status_code in (401, 403)


async def test_status_admin_cannot_query_other_tenant(client, unique_slug):
    """[SECURITE] Un admin ne doit jamais pouvoir inspecter le statut Stripe
    d'un AUTRE restaurant en passant ?tenant_slug= -- reserve au super-admin."""
    slug = f"pc{unique_slug}"
    admin = await _register_admin(client, slug)
    headers = {"Authorization": f"Bearer {admin['access_token']}"}

    with patch(
        "app.modules.payments.connect_router.connect_service.get_connect_status"
    ) as get_status:
        resp = await client.get(
            "/api/v1/payments/connect/status",
            params={"tenant_slug": "un-autre-restaurant"},
            headers=headers,
        )

    assert resp.status_code == 403
    get_status.assert_not_called()


async def test_dashboard_admin_cannot_query_other_tenant(client, unique_slug):
    slug = f"pd{unique_slug}"
    admin = await _register_admin(client, slug)
    headers = {"Authorization": f"Bearer {admin['access_token']}"}

    with patch(
        "app.modules.payments.connect_router.connect_service.get_dashboard_link"
    ) as get_link:
        resp = await client.get(
            "/api/v1/payments/connect/dashboard",
            params={"tenant_slug": "un-autre-restaurant"},
            headers=headers,
        )

    assert resp.status_code == 403
    get_link.assert_not_called()


async def test_status_super_admin_can_query_other_tenant(client, unique_slug):
    """Le super-admin, lui, a le droit d'inspecter n'importe quel tenant."""
    slug = f"ps{unique_slug}"
    await _register_admin(client, slug)

    fake_status = {
        "stripe_account_id": "acct_probe",
        "details_submitted": True,
        "payouts_enabled": True,
        "charges_enabled": True,
        "onboarding_complete": True,
    }
    with (
        patch(
            "app.modules.payments.connect_router.connect_service.get_connect_status",
            new=AsyncMock(return_value=fake_status),
        ) as get_status,
        # Identite super-admin verifiee separement (voir tests/test_super_admin_auth.py) --
        # ce test porte sur la frontiere d'autorisation Connect, pas sur cette verification.
        patch(
            "app.core.auth.super_admin.super_admin_exists",
            new=AsyncMock(return_value=True),
        ),
    ):
        resp = await client.get(
            "/api/v1/payments/connect/status",
            params={"tenant_slug": slug},
            headers=_super_admin_headers(),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["stripe_account_id"] == "acct_probe"
    get_status.assert_called_once_with(slug)


async def test_status_admin_can_query_own_tenant_without_param(client, unique_slug):
    """Chemin heureux : un admin sans ?tenant_slug= consulte son propre restaurant."""
    slug = f"po{unique_slug}"
    admin = await _register_admin(client, slug)
    headers = {"Authorization": f"Bearer {admin['access_token']}"}

    fake_status = {
        "stripe_account_id": None,
        "details_submitted": False,
        "payouts_enabled": False,
        "charges_enabled": False,
        "onboarding_complete": False,
    }
    with patch(
        "app.modules.payments.connect_router.connect_service.get_connect_status",
        new=AsyncMock(return_value=fake_status),
    ) as get_status:
        resp = await client.get("/api/v1/payments/connect/status", headers=headers)

    assert resp.status_code == 200, resp.text
    get_status.assert_called_once_with(slug)
