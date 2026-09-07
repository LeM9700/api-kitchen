"""Tests -- atomicité audit/action pour les actions fail-closed Super Admin
(Correction 2, suite du durcissement Super Admin).

Contexte : ``create_tenant`` effectuait le provisioning complet (ligne
``public.tenants``, schéma, tables, premier admin) PUIS écrivait l'événement
``tenant_created`` dans ``public.platform_audit_logs`` DANS UNE TRANSACTION
SÉPARÉE. Un échec de cet audit laissait un tenant entièrement fonctionnel
sans aucune trace de qui l'avait créé.

Correctif : ``provision_tenant()`` accepte désormais un callback
``on_provisioned`` exécuté DANS LA MÊME transaction PostgreSQL, juste avant
le commit (voir app/core/tenancy/provisioning.py). PostgreSQL supporte le DDL
transactionnel : un ROLLBACK annule aussi bien un ``CREATE SCHEMA`` qu'un
``INSERT`` -- si l'audit échoue, TOUT le provisioning est annulé.
"""
import uuid
from unittest.mock import patch

import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import text

from app.core.auth.security import create_access_token, compute_token_lookup
from app.core.config import settings
from app.core.database import get_public_session, tenant_schema_name
from app.main import app as fastapi_app

pytestmark = pytest.mark.asyncio


async def _create_super_admin(email: str) -> int:
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, 'unused-hash', true) RETURNING id"
            ),
            {"email": email},
        )
        admin_id = result.scalar_one()
        await session.commit()
        return admin_id


async def _create_super_admin_session(admin_id: int) -> str:
    from datetime import datetime, timedelta, timezone

    sid = str(uuid.uuid4())
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admin_sessions "
                "(id, super_admin_id, refresh_token_lookup, expires_at) "
                "VALUES (:sid, :admin_id, :lookup, :expires_at)"
            ),
            {
                "sid": sid,
                "admin_id": admin_id,
                "lookup": compute_token_lookup(f"unused-{sid}"),
                "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
            },
        )
        await session.commit()
    return sid


async def _super_admin_token(admin_id: int, email: str) -> str:
    sid = await _create_super_admin_session(admin_id)
    return create_access_token({
        "sub": str(admin_id),
        "email": email,
        "role": "super-admin",
        "tenant_slug": None,
        "tenant_id": None,
        "sid": sid,
        "auth_version": 1,
    })


async def _schema_exists(db_engine, schema: str) -> bool:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": schema},
        )
        return result.scalar_one_or_none() is not None


async def test_tenant_created_audit_failure_rolls_back_everything(client, db_engine):
    """[SECURITE] Coeur de la Correction 2 : si l'ecriture d'audit
    tenant_created echoue, AUCUNE ligne public.tenants, AUCUN schema, AUCUN
    admin ne doit survivre -- la transaction complete est annulee."""
    unique = uuid.uuid4().hex[:8]
    slug = f"audit-fail-{unique}"
    admin_email = f"admin-{unique}@test.com"
    super_admin_email = f"super-{unique}@test.com"

    admin_id = await _create_super_admin(super_admin_email)
    token = await _super_admin_token(admin_id, super_admin_email)

    with patch(
        "app.modules.admin.tenants.lifecycle_router.record_platform_audit_event",
        side_effect=RuntimeError("simulated audit outage"),
    ), pytest.raises(RuntimeError, match="simulated audit outage"):
        await client.post(
            "/api/v1/admin/tenants",
            json={"slug": slug, "name": "Audit Fail Tenant", "admin_email": admin_email},
            headers={"Authorization": f"Bearer {token}"},
        )

    async with get_public_session() as session:
        tenant_row = await session.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": slug}
        )
        assert tenant_row.scalar_one_or_none() is None, "aucune ligne public.tenants ne doit subsister"

        # L'admin est cree dans le schema TENANT (jamais public.users) --
        # verifie via l'absence meme du schema, plus fort que verifier la table.
    assert not await _schema_exists(db_engine, tenant_schema_name(slug)), (
        "le schema tenant ne doit pas exister apres un rollback complet"
    )

    async with get_public_session() as session:
        audit_row = await session.execute(
            text("SELECT id FROM public.platform_audit_logs WHERE target_id = :slug"),
            {"slug": slug},
        )
        assert audit_row.scalar_one_or_none() is None, "aucun evenement d'audit partiel ne doit subsister"


async def test_tenant_created_success_persists_tenant_and_audit_together(client, db_engine):
    """Chemin heureux : le tenant complet ET l'evenement d'audit sont presents."""
    unique = uuid.uuid4().hex[:8]
    slug = f"audit-ok-{unique}"
    admin_email = f"admin-{unique}@test.com"
    super_admin_email = f"super-{unique}@test.com"

    admin_id = await _create_super_admin(super_admin_email)
    token = await _super_admin_token(admin_id, super_admin_email)

    try:
        resp = await client.post(
            "/api/v1/admin/tenants",
            json={"slug": slug, "name": "Audit OK Tenant", "admin_email": admin_email},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text

        async with get_public_session() as session:
            tenant_row = await session.execute(
                text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": slug}
            )
            assert tenant_row.scalar_one_or_none() is not None

            audit_row = await session.execute(
                text(
                    "SELECT event_type, actor_super_admin_id FROM public.platform_audit_logs "
                    "WHERE target_id = :slug AND event_type = 'tenant_created'"
                ),
                {"slug": slug},
            )
            row = audit_row.first()
            assert row is not None
            assert row.actor_super_admin_id == admin_id

        assert await _schema_exists(db_engine, tenant_schema_name(slug))
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant_schema_name(slug)}" CASCADE'))
            await conn.execute(text("DELETE FROM public.platform_audit_logs WHERE target_id = :s"), {"s": slug})
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": slug})


async def test_tenant_suspend_audit_failure_rolls_back_suspension(client, unique_slug):
    """[SECURITE] Action fail-closed EXISTANTE (suspension) : si son audit
    echoue, is_suspended ne doit PAS rester a true -- deja atomique (meme
    transaction/session) mais verifie ici explicitement en base reelle."""
    reg_resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": unique_slug,
        "tenant_name": "Suspend Audit Tenant",
        "email": f"admin-{unique_slug}@test.com",
        "password": "Valid1!aa",
    })
    assert reg_resp.status_code == 201, reg_resp.text

    async with get_public_session() as session:
        tenant_id = await session.scalar(
            text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": unique_slug}
        )

    super_admin_email = f"super-suspend-{unique_slug}@test.com"
    admin_id = await _create_super_admin(super_admin_email)
    token = await _super_admin_token(admin_id, super_admin_email)

    # [SECURITE] Un vrai client Redis est requis ici : get_current_user()
    # consulte request.app.state.arq_pool pour la deny-list JTI -- un mock
    # generique (AsyncMock) renverrait une valeur tronquee en booleen a True
    # pour n'importe quel appel (y compris is_jti_revoked), rejetant a tort
    # le token comme "revoque".
    redis_client = redis_asyncio.from_url(settings.redis_url)
    fastapi_app.state.arq_pool = redis_client
    try:
        with patch(
            "app.modules.admin.tenants.lifecycle_router.record_platform_audit_event",
            side_effect=RuntimeError("simulated audit outage"),
        ), pytest.raises(RuntimeError, match="simulated audit outage"):
            await client.patch(
                f"/api/v1/admin/tenants/{tenant_id}/suspend",
                json={"suspend": True, "suspension_message": "test"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        fastapi_app.state.arq_pool = None
        await redis_client.aclose()

    async with get_public_session() as session:
        row = await session.execute(
            text("SELECT is_suspended FROM public.tenants WHERE id = :id"), {"id": tenant_id}
        )
        assert row.scalar_one() is False, (
            "is_suspended doit rester false : l'echec d'audit doit annuler la suspension"
        )
