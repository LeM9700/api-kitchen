"""Configuration pytest et fixtures partagees.

Strategie d'isolation DB :
    Chaque test recoit une ``AsyncSession`` liee a une connexion dont la
    transaction englobante est rollbackee en teardown. La session utilise
    ``join_transaction_mode="create_savepoint"`` : chaque ``session.commit()``
    libere le savepoint courant et en ouvre un nouveau, sans jamais toucher
    la transaction englobante. Le ``conn.rollback()`` final annule tout.

    [PROD] Ce pattern ne fonctionne qu'avec PostgreSQL (SAVEPOINT) et asyncpg.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.auth.security import create_access_token
from app.core.config import settings
from app.core.database import tenant_schema_name
from app.main import app
from app.modules.auth.service import _provision_tenant_schema


@pytest.fixture(scope="session")
async def db_engine():
    """Moteur SQLAlchemy async partage sur toute la session de test.

    Cree une seule fois pour eviter le cout de la connexion pool a chaque test.
    Utilise ``test_database_url`` si configure, sinon ``database_url``.
    """
    engine = create_async_engine(
        settings.test_database_url or settings.database_url,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Session DB isolee par test via nested transaction (savepoint) rollback.

    Cycle de vie :
        1. Ouvre une connexion et demarre une transaction englobante.
        2. Cree une ``AsyncSession`` avec ``join_transaction_mode="create_savepoint"``
           : chaque ``session.commit()`` libere le savepoint courant et en cree
           un nouveau, sans jamais emettre de vrai COMMIT vers PostgreSQL.
        3. Au teardown, ``conn.rollback()`` annule l'integralite des operations
           du test, quelle que soit leur profondeur.

    Scope : ``function`` -- chaque test repart d'un etat DB propre.

    [PROD] Les fixtures de seed (tenant, user) doivent dependre de
    ``db_session`` pour etre incluses dans le rollback.

    Yields:
        AsyncSession liee a la transaction englobante rollbackee en teardown.
    """
    async with db_engine.connect() as conn:
        await conn.begin()
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


@pytest.fixture(scope="session", autouse=True)
async def bootstrap_test_tenants(db_engine):
    """Provisionne les schemas tenants utilises par la suite de tests."""
    tenant_specs = (
        ("default", "Default Tenant"),
        ("pizza_test", "Pizza Test Tenant"),
        ("test", "Test Tenant"),
    )
    async with db_engine.begin() as conn:
        for slug, name in tenant_specs:
            schema = tenant_schema_name(slug)
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            await conn.execute(
                text(
                    """
                    INSERT INTO public.tenants (slug, name, plan)
                    VALUES (:slug, :name, 'starter')
                    ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
                    """
                ),
                {"slug": slug, "name": name},
            )
            await _provision_tenant_schema(conn, slug)
            await conn.execute(text(f'SET search_path TO "{schema}", public'))
            await conn.execute(
                text(
                    """
                    INSERT INTO users (email, password_hash, full_name, role, is_active)
                    VALUES (:email, :password_hash, :full_name, 'admin', true)
                    ON CONFLICT (email) DO UPDATE
                    SET role = 'admin', is_active = true
                    """
                ),
                {
                    "email": f"admin@{slug}.test",
                    "password_hash": "not-used-in-tests",
                    "full_name": f"Admin {slug}",
                },
            )
            await conn.execute(text("SET search_path TO public"))


@pytest.fixture
async def public_session(db_session):
    """Alias retrocompatible de ``db_session`` pour les tests du schema public.

    Yields:
        La meme session isolee que ``db_session``.
    """
    yield db_session


@pytest.fixture
async def client():
    """Client HTTP async branche directement sur l'application ASGI.

    N'utilise pas la DB isolee car les endpoints gerent leur propre session
    via ``get_tenant_session`` / ``get_public_session``. A n'utiliser que pour
    les tests qui ne necessitent pas de controle fin de la transaction.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def unique_slug() -> str:
    """Suffixe court unique par test, pour les tenants crees via ``client``.

    ``client`` commit reellement en base (pas de rollback) : un tenant_slug/email
    fixe reste enregistre entre deux executions locales de la suite contre le
    meme conteneur Postgres persistant, ce qui casse l'idempotence des tests
    (409 sur re-inscription, schema tenant fige avant une migration recente).
    """
    return uuid.uuid4().hex[:8]


@pytest.fixture
def demo_tenant_slug() -> str:
    return "default"


@pytest.fixture
async def authed_client(db_engine, demo_tenant_slug: str):
    async with db_engine.connect() as conn:
        tenant_id = (
            await conn.execute(
                text("SELECT id FROM public.tenants WHERE slug = :slug"),
                {"slug": demo_tenant_slug},
            )
        ).scalar_one()
        admin_email = f"admin@{demo_tenant_slug}.test"
        schema = tenant_schema_name(demo_tenant_slug)
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        admin_user = (
            await conn.execute(
                text("SELECT id, email FROM users WHERE email = :email LIMIT 1"),
                {"email": admin_email},
            )
        ).mappings().first()
        if admin_user is None:
            raise RuntimeError(f"Admin user not found for tenant {demo_tenant_slug}")
        await conn.execute(text("SET search_path TO public"))

    token = create_access_token(
        {
            "sub": str(admin_user["id"]),
            "tenant_id": tenant_id,
            "tenant_slug": demo_tenant_slug,
            "role": "admin",
            "email": admin_user["email"],
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": " ".join(("Bearer", token))},
    ) as test_client:
        yield test_client
