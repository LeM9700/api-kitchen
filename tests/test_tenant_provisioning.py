"""Provisioning unique, complet et atomique des tenants PostgreSQL (Prompt 02).

Contexte : avant ce correctif, le flux d'inscription standard
(``POST /auth/register``) et le flux Super Admin (``POST /admin/tenants``)
provisionnaient des tenants DIFFERENTS -- le second creait un schema vide et
un admin sans jamais appeler le provisionneur de tables, et cet admin
s'inserait meme silencieusement dans la table historique ``public.users``
(schema tenant vide -> repli via l'ancien search_path ``tenant_x, public``).
Les deux parcours utilisent maintenant le meme service unique
(``app/core/tenancy/provisioning.py::provision_tenant``), qui effectue
insertion + schema + tables + donnees minimales + premier admin dans UNE
seule transaction PostgreSQL.

Ce fichier verifie, avec une vraie base PostgreSQL :
    1. L'inscription standard produit un tenant complet.
    2. La creation Super Admin produit un tenant complet (regression test du
       bug ci-dessus : avant ce correctif, ce test aurait echoue).
    3. Les deux parcours produisent des structures identiques.
    4. Un echec volontaire pendant le provisioning annule TOUT (pas de ligne,
       pas de schema, pas d'admin partiel).
    5. Une table tenant absente ne se replie JAMAIS silencieusement vers
       ``public`` (le risque documente dans get_tenant_session).
    6. Un tenant existant n'est jamais affecte par l'echec du provisioning
       d'un AUTRE tenant.
"""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth.security import compute_token_lookup, create_access_token
from app.core.database import get_public_session, get_tenant_session, tenant_schema_name
from app.core.tenancy.provisioning import provision_tenant
from app.modules.auth.models import User
from tools.audit_tenant_schemas import EXPECTED_TENANT_TABLES


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
    """Insere une session public.super_admin_sessions active -- requise
    depuis le durcissement MFA/sessions (voir app.core.auth.super_admin)."""
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


async def _actual_tables(conn, schema: str) -> set[str]:
    result = await conn.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
        {"s": schema},
    )
    return {row.table_name for row in result}


async def _columns(conn, schema: str) -> dict:
    result = await conn.execute(
        text(
            """SELECT table_name, column_name, data_type, is_nullable
               FROM information_schema.columns WHERE table_schema = :s"""
        ),
        {"s": schema},
    )
    return {(r.table_name, r.column_name): (r.data_type, r.is_nullable) for r in result}


# ---------------------------------------------------------------------------
# 1-2. Les deux parcours produisent un tenant COMPLET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_endpoint_produces_complete_tenant(client, db_engine, unique_slug):
    """L'inscription standard cree un schema avec TOUTES les tables
    attendues, et l'admin atterrit dans le schema tenant -- jamais dans
    ``public.users``."""
    email = f"complete-{unique_slug}@test.com"
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": unique_slug,
        "tenant_name": "Complete Register Tenant",
        "email": email,
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text

    schema = tenant_schema_name(unique_slug)
    async with db_engine.connect() as conn:
        actual = await _actual_tables(conn, schema)
        assert EXPECTED_TENANT_TABLES <= actual, f"tables manquantes: {EXPECTED_TENANT_TABLES - actual}"

        public_count = await conn.scalar(
            text("SELECT count(*) FROM public.users WHERE email = :email"), {"email": email}
        )
        assert public_count == 0, "l'admin ne doit JAMAIS atterrir dans public.users"

        tenant_count = await conn.scalar(
            text(f'SELECT count(*) FROM "{schema}".users WHERE email = :email'), {"email": email}
        )
        assert tenant_count == 1


@pytest.mark.asyncio
async def test_super_admin_create_tenant_produces_complete_tenant(client, db_engine):
    """Regression test du bug corrige par ce Prompt 02 : avant ce correctif,
    POST /admin/tenants ne provisionnait AUCUNE table applicative (schema
    vide) et l'admin atterrissait silencieusement dans ``public.users``."""
    unique = uuid.uuid4().hex[:8]
    slug = f"superadmin-complete-{unique}"
    admin_email = f"admin-{unique}@test.com"
    super_admin_email = f"super-{unique}@test.com"

    admin_id = await _create_super_admin(super_admin_email)
    token = await _super_admin_token(admin_id, super_admin_email)

    try:
        resp = await client.post(
            "/api/v1/admin/tenants",
            json={"slug": slug, "name": "Complete SuperAdmin Tenant", "admin_email": admin_email},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text

        schema = tenant_schema_name(slug)
        async with db_engine.connect() as conn:
            actual = await _actual_tables(conn, schema)
            assert EXPECTED_TENANT_TABLES <= actual, f"tables manquantes: {EXPECTED_TENANT_TABLES - actual}"

            public_count = await conn.scalar(
                text("SELECT count(*) FROM public.users WHERE email = :email"), {"email": admin_email}
            )
            assert public_count == 0, "l'admin ne doit JAMAIS atterrir dans public.users"

            tenant_count = await conn.scalar(
                text(f'SELECT count(*) FROM "{schema}".users WHERE email = :email'), {"email": admin_email}
            )
            assert tenant_count == 1
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant_schema_name(slug)}" CASCADE'))
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": slug})
            await conn.execute(text("DELETE FROM public.super_admins WHERE id = :id"), {"id": admin_id})


# ---------------------------------------------------------------------------
# 3. Parite structurelle entre les deux parcours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_and_super_admin_schemas_are_structurally_identical(client, db_engine):
    """Un tenant cree par inscription standard et un tenant cree par
    Super Admin doivent avoir EXACTEMENT les memes tables et colonnes."""
    unique = uuid.uuid4().hex[:8]
    register_slug = f"parity-register-{unique}"
    superadmin_slug = f"parity-superadmin-{unique}"
    super_admin_email = f"super-parity-{unique}@test.com"

    admin_id = await _create_super_admin(super_admin_email)
    token = await _super_admin_token(admin_id, super_admin_email)

    try:
        reg_resp = await client.post("/api/v1/auth/register", json={
            "tenant_slug": register_slug,
            "tenant_name": "Parity Register",
            "email": f"parity-{unique}@test.com",
            "password": "Valid1!aa",
        })
        assert reg_resp.status_code == 201, reg_resp.text

        sa_resp = await client.post(
            "/api/v1/admin/tenants",
            json={
                "slug": superadmin_slug,
                "name": "Parity SuperAdmin",
                "admin_email": f"parity-admin-{unique}@test.com",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert sa_resp.status_code == 201, sa_resp.text

        register_schema = tenant_schema_name(register_slug)
        superadmin_schema = tenant_schema_name(superadmin_slug)

        async with db_engine.connect() as conn:
            register_tables = await _actual_tables(conn, register_schema)
            superadmin_tables = await _actual_tables(conn, superadmin_schema)
            register_cols = await _columns(conn, register_schema)
            superadmin_cols = await _columns(conn, superadmin_schema)

        assert register_tables == superadmin_tables
        assert register_cols == superadmin_cols
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant_schema_name(superadmin_slug)}" CASCADE'))
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": superadmin_slug})
            await conn.execute(text("DELETE FROM public.super_admins WHERE id = :id"), {"id": admin_id})


# ---------------------------------------------------------------------------
# 4. Echec volontaire pendant le provisioning -> rollback integral
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_tenant_forced_failure_during_admin_creation_rolls_back_everything(db_engine):
    """Force un echec APRES la creation du schema et des tables (contrainte
    NOT NULL violee sur le premier admin : ``password_hash`` manquant) et
    verifie que TOUT est annule -- pas de ligne public.tenants, pas de schema,
    aucune compensation applicative necessaire (pur ROLLBACK PostgreSQL)."""
    unique = uuid.uuid4().hex[:8]
    slug = f"forced-failure-{unique}"
    schema = tenant_schema_name(slug)

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": slug})

    with pytest.raises(DBAPIError):
        await provision_tenant(
            slug=slug,
            name="Forced Failure Tenant",
            admin_fields={"email": "admin@forced-failure.test"},  # password_hash manquant (NOT NULL)
        )

    async with db_engine.connect() as conn:
        tenant_count = await conn.scalar(
            text("SELECT count(*) FROM public.tenants WHERE slug = :s"), {"s": slug}
        )
        schema_exists = await conn.scalar(
            text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": schema}
        )
    assert tenant_count == 0, "aucune ligne public.tenants ne doit survivre a l'echec"
    assert schema_exists == 0, "aucun schema (meme complet) ne doit survivre a l'echec"


# ---------------------------------------------------------------------------
# 5. Pas de repli silencieux vers public pour une table tenant manquante
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_session_never_falls_back_to_public_for_missing_table(db_engine, unique_slug):
    """Un schema tenant auquel il manque la table ``users`` (provisioning
    interrompu, migration jamais appliquee) doit faire echouer une requete
    ORM standard -- jamais lire silencieusement ``public.users`` (table
    historique homonyme, migration 0002)."""
    schema = tenant_schema_name(unique_slug)
    marker_email = f"public-leak-marker-{unique_slug}@test.com"

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": unique_slug})
        await conn.scalar(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, 'starter') RETURNING id"
            ),
            {"slug": unique_slug, "name": "Missing Users Table Tenant"},
        )
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        from app.core.tenancy.provisioning import _provision_tenant_schema

        # Provisionne TOUT normalement puis retire la table users --
        # reproduit un schema incomplet (provisioning interrompu, migration
        # jamais appliquee) plus fidelement qu'un create_all partiel (qui
        # violerait les FK d'autres tables vers users).
        await _provision_tenant_schema(conn, unique_slug)
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.execute(text("DROP TABLE users CASCADE"))
        await conn.execute(text("SET search_path TO public"))

        # Un utilisateur avec le MEME email existe dans la table historique
        # public.users -- si un repli silencieux se produisait, cette requete
        # le trouverait a tort.
        await conn.execute(
            text(
                "INSERT INTO public.users (email, password_hash, role, is_active) "
                "VALUES (:email, 'x', 'admin', true) "
                "ON CONFLICT DO NOTHING"
            ),
            {"email": marker_email},
        )

    try:
        with pytest.raises(DBAPIError):
            async with get_tenant_session(unique_slug) as session:
                from sqlalchemy.future import select

                await session.execute(select(User).where(User.email == marker_email))
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": unique_slug})
            await conn.execute(text("DELETE FROM public.users WHERE email = :e"), {"e": marker_email})


# ---------------------------------------------------------------------------
# 6. Un tenant existant est intact apres l'echec d'un AUTRE tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_tenant_untouched_after_unrelated_tenant_provisioning_failure(
    client, db_engine, unique_slug
):
    """Provisionne un tenant A reel et fonctionnel, puis force l'echec du
    provisioning d'un tenant B SANS RAPPORT (contrainte violee sur son
    admin) -- verifie que les donnees de A (ligne public.tenants, schema,
    contenu) ne sont affectees d'AUCUNE maniere."""
    email_a = f"tenant-a-{unique_slug}@test.com"
    reg_resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": unique_slug,
        "tenant_name": "Tenant A Untouched",
        "email": email_a,
        "password": "Valid1!aa",
    })
    assert reg_resp.status_code == 201, reg_resp.text

    schema_a = tenant_schema_name(unique_slug)
    async with db_engine.connect() as conn:
        tenant_a_row_before = dict(
            (
                await conn.execute(
                    text("SELECT id, slug, name, plan FROM public.tenants WHERE slug = :s"),
                    {"s": unique_slug},
                )
            ).mappings().one()
        )
        admin_a_before = dict(
            (
                await conn.execute(
                    text(f'SELECT id, email, role FROM "{schema_a}".users WHERE email = :e'),
                    {"e": email_a},
                )
            ).mappings().one()
        )

    unique_b = uuid.uuid4().hex[:8]
    slug_b = f"unrelated-failure-{unique_b}"
    with pytest.raises(DBAPIError):
        await provision_tenant(
            slug=slug_b,
            name="Unrelated Failing Tenant",
            admin_fields={"email": "admin@unrelated-failure.test"},  # password_hash manquant
        )

    async with db_engine.connect() as conn:
        tenant_a_row_after = dict(
            (
                await conn.execute(
                    text("SELECT id, slug, name, plan FROM public.tenants WHERE slug = :s"),
                    {"s": unique_slug},
                )
            ).mappings().one()
        )
        admin_a_after = dict(
            (
                await conn.execute(
                    text(f'SELECT id, email, role FROM "{schema_a}".users WHERE email = :e'),
                    {"e": email_a},
                )
            ).mappings().one()
        )
        b_row_count = await conn.scalar(
            text("SELECT count(*) FROM public.tenants WHERE slug = :s"), {"s": slug_b}
        )

    assert tenant_a_row_after == tenant_a_row_before
    assert admin_a_after == admin_a_before
    assert b_row_count == 0
