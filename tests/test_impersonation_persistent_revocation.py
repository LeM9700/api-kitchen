"""Tests -- révocation d'impersonation persistée en PostgreSQL (Correction 3).

Contexte : la révocation d'un token d'impersonation reposait auparavant
UNIQUEMENT sur la deny-list Redis (``jti``). Si Redis était absent ou
indisponible, ``/impersonation/end`` pouvait écrire un audit
"impersonation_ended" alors que le token restait, en réalité, valide jusqu'à
expiration -- la trace d'audit mentait sur l'état réel de sécurité.

Correctif : ``public.super_admin_impersonation_sessions`` est la source de
vérité persistante (voir app.core.auth.impersonation). Chaque token porte un
claim ``impersonation_id`` référençant une ligne de cette table ;
``validate_impersonation_token`` refuse tout token dont l'enregistrement est
absent, expiré ou ``revoked_at`` non nul -- indépendamment de Redis, qui
reste un pur accélérateur de révocation immédiate (best-effort, après
commit, jamais requis).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pyotp
import pytest
from sqlalchemy import text

from app.core.auth.security import get_password_hash
from app.core.database import get_public_session

pytestmark = pytest.mark.asyncio

_PASSWORD = "correct horse battery staple"


async def _create_full_super_admin_session(client, email: str) -> dict:
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, :hash, true)"
            ),
            {"email": email, "hash": get_password_hash(_PASSWORD)},
        )
        await session.commit()

    login_resp = await client.post(
        "/api/v1/super-admin/login", json={"email": email, "password": _PASSWORD}
    )
    enrollment_token = login_resp.json()["access_token"]
    setup_resp = await client.post(
        "/api/v1/super-admin/mfa/setup",
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )
    secret = setup_resp.json()["secret"]
    await client.post(
        "/api/v1/super-admin/mfa/confirm",
        json={"totp_code": pyotp.TOTP(secret).now()},
        headers={"Authorization": f"Bearer {enrollment_token}"},
    )

    full_login_resp = await client.post(
        "/api/v1/super-admin/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )
    assert full_login_resp.status_code == 200, full_login_resp.text
    return full_login_resp.json()


async def _impersonate(client, admin_session: dict, tenant_slug: str) -> str:
    resp = await client.post(
        f"/api/v1/admin/impersonate/{tenant_slug}",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _decode_impersonation_id(token: str) -> str:
    import jwt as pyjwt

    from app.core.config import settings

    payload = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    return payload["impersonation_id"]


async def test_impersonation_token_valid_before_revocation(
    client, unique_slug, demo_tenant_slug
):
    email = f"persist-valid-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    token = await _impersonate(client, admin_session, demo_tenant_slug)

    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    impersonation_id = _decode_impersonation_id(token)
    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT revoked_at, expires_at > now() AS not_expired "
                "FROM public.super_admin_impersonation_sessions WHERE id = :id"
            ),
            {"id": impersonation_id},
        )
        record = row.first()
        assert record is not None
        assert record.revoked_at is None
        assert record.not_expired is True


async def test_impersonation_end_revokes_even_without_redis(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] Coeur de la Correction 3 : SANS Redis configuré
    (app.state.arq_pool absent), /impersonation/end doit tout de même rendre
    le token définitivement inutilisable -- la persistance PostgreSQL est la
    source de vérité, pas un accélérateur optionnel."""
    email = f"persist-norealdis-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    token = await _impersonate(client, admin_session, demo_tenant_slug)

    # Pas de app.state.arq_pool positionné dans ce test -- getattr(...) sera None.
    end_resp = await client.post(
        "/api/v1/admin/impersonation/end",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert end_resp.status_code == 204, end_resp.text

    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text

    impersonation_id = _decode_impersonation_id(token)
    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT revoked_at, reason FROM public.super_admin_impersonation_sessions "
                "WHERE id = :id"
            ),
            {"id": impersonation_id},
        )
        record = row.first()
        assert record.revoked_at is not None
        assert record.reason == "ended_by_user"

        audit_row = await session.execute(
            text(
                "SELECT 1 FROM public.platform_audit_logs "
                "WHERE event_type = 'impersonation_ended' "
                "AND metadata->>'impersonation_id' = :id"
            ),
            {"id": impersonation_id},
        )
        assert audit_row.scalar_one_or_none() is not None, (
            "l'audit ended doit bien être écrit (révocation réussie, pas un faux audit)"
        )


async def test_impersonation_rejected_after_source_session_revoked(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] La révocation de la session Super Admin source invalide
    aussi l'impersonation, sans jamais appeler /impersonation/end."""
    email = f"persist-srcrevoked-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    token = await _impersonate(client, admin_session, demo_tenant_slug)

    logout_resp = await client.post(
        "/api/v1/super-admin/logout",
        headers={"Authorization": f"Bearer {admin_session['access_token']}"},
    )
    assert logout_resp.status_code == 204, logout_resp.text

    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text

    # [SECURITE] L'enregistrement persistant lui-même reste non-révoqué --
    # c'est bien la session source qui bloque, pas un état incohérent.
    impersonation_id = _decode_impersonation_id(token)
    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT revoked_at FROM public.super_admin_impersonation_sessions WHERE id = :id"
            ),
            {"id": impersonation_id},
        )
        assert row.scalar_one() is None


async def test_impersonation_end_audit_failure_leaves_revocation_uncommitted(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] revoked_at et l'audit sont écrits DANS LA MÊME transaction --
    si l'audit échoue, revoked_at reste NULL et le token reste valide
    (aucune révocation ni audit partiel)."""
    email = f"persist-atomic-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    token = await _impersonate(client, admin_session, demo_tenant_slug)
    impersonation_id = _decode_impersonation_id(token)

    with patch(
        "app.modules.admin.superadmin.router.record_platform_audit_event",
        side_effect=RuntimeError("simulated audit outage"),
    ), pytest.raises(RuntimeError, match="simulated audit outage"):
        await client.post(
            "/api/v1/admin/impersonation/end",
            headers={"Authorization": f"Bearer {token}"},
        )

    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT revoked_at FROM public.super_admin_impersonation_sessions WHERE id = :id"
            ),
            {"id": impersonation_id},
        )
        assert row.scalar_one() is None, "revoked_at doit rester NULL si l'audit a échoué"

        audit_row = await session.execute(
            text(
                "SELECT 1 FROM public.platform_audit_logs "
                "WHERE event_type = 'impersonation_ended' "
                "AND metadata->>'impersonation_id' = :id"
            ),
            {"id": impersonation_id},
        )
        assert audit_row.scalar_one_or_none() is None, "aucun audit ended ne doit subsister"

    # Le token reste donc parfaitement utilisable -- aucune révocation partielle.
    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text


async def test_impersonation_record_expiry_is_enforced_independently(
    client, unique_slug, demo_tenant_slug
):
    """[SECURITE] "expiration ... de l'enregistrement" : même si le JWT lui-même
    n'a pas encore expiré, expires_at dans l'enregistrement persistant est
    revérifié à chaque requête -- une défense en profondeur indépendante du
    seul décodage JWT."""
    email = f"persist-expiry-{unique_slug}@test.com"
    admin_session = await _create_full_super_admin_session(client, email)
    token = await _impersonate(client, admin_session, demo_tenant_slug)
    impersonation_id = _decode_impersonation_id(token)

    async with get_public_session() as session:
        await session.execute(
            text(
                "UPDATE public.super_admin_impersonation_sessions "
                "SET expires_at = :past WHERE id = :id"
            ),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": impersonation_id},
        )
        await session.commit()

    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text
