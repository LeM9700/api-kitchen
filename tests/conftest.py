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
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth.security import create_access_token, get_password_hash
from app.core.config import settings
from app.core.database import tenant_schema_name
from app.core.http.limiter import limiter
from app.main import app
from app.modules.auth.service import _provision_tenant_schema

DEFAULT_TEST_TENANT_SLUG = "test"
LEGACY_TEST_TENANT_SLUG = "pizza_test"
DEFAULT_INTEGRATION_TENANT_SLUG = "default"
DEFAULT_TEST_ADMIN_EMAIL = "admin@test.com"
DEFAULT_TEST_STAFF_EMAIL = "staff@test.com"
DEFAULT_TEST_CLIENT_EMAIL = "client@test.com"


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


@pytest.fixture(scope="session", autouse=True)
async def bootstrap_default_tenant(db_engine):
    """Provisionne des tenants de démo frais pour les tests tenant-scoped.

    La CI exécute Alembic sur une base neuve : sans tenant préexistant,
    aucune migration tenant-scoped historique ne peut boucler sur
    ``public.tenants``. On recrée donc explicitement deux tenants stables :
    ``test`` pour les fixtures génériques et ``pizza_test`` pour les tests
    historiques qui ciblent ce schéma en dur.
    """
    async with db_engine.begin() as conn:
        tenant_ids = {}
        for slug, name in (
            (DEFAULT_TEST_TENANT_SLUG, "Test Tenant"),
            (DEFAULT_INTEGRATION_TENANT_SLUG, "Default Tenant"),
            (LEGACY_TEST_TENANT_SLUG, "Pizza Test"),
        ):
            tenant_ids[slug] = await conn.scalar(
                text(
                    """INSERT INTO public.tenants (slug, name, plan)
                       VALUES (:slug, :name, 'starter')
                       ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
                       RETURNING id"""
                ),
                {"slug": slug, "name": name},
            )
            schema = tenant_schema_name(slug)
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await _provision_tenant_schema(conn, slug)

        await conn.execute(text(f'SET search_path TO "{tenant_schema_name(DEFAULT_TEST_TENANT_SLUG)}", public'))
        demo_admin_user_id = await conn.scalar(
            text(
                """INSERT INTO users (
                       email, password_hash, role, is_active, email_verified_at
                   )
                   VALUES (:email, :password_hash, :role, true, now())
                   ON CONFLICT (email) DO UPDATE
                   SET role = EXCLUDED.role,
                       is_active = EXCLUDED.is_active,
                       email_verified_at = COALESCE(users.email_verified_at, EXCLUDED.email_verified_at)
                   RETURNING id"""
            ),
            {
                "email": DEFAULT_TEST_ADMIN_EMAIL,
                "password_hash": get_password_hash("not-used-in-tests"),
                "role": "admin",
            },
        )
        demo_staff_user_id = await conn.scalar(
            text(
                """INSERT INTO users (
                       email, password_hash, role, is_active, email_verified_at
                   )
                   VALUES (:email, :password_hash, :role, true, now())
                   ON CONFLICT (email) DO UPDATE
                   SET role = EXCLUDED.role,
                       is_active = EXCLUDED.is_active,
                       email_verified_at = COALESCE(users.email_verified_at, EXCLUDED.email_verified_at)
                   RETURNING id"""
            ),
            {
                "email": DEFAULT_TEST_STAFF_EMAIL,
                "password_hash": get_password_hash("not-used-in-tests"),
                "role": "staff",
            },
        )
        demo_client_user_id = await conn.scalar(
            text(
                """INSERT INTO users (
                       email, password_hash, role, is_active, email_verified_at
                   )
                   VALUES (:email, :password_hash, :role, true, now())
                   ON CONFLICT (email) DO UPDATE
                   SET role = EXCLUDED.role,
                       is_active = EXCLUDED.is_active,
                       email_verified_at = COALESCE(users.email_verified_at, EXCLUDED.email_verified_at)
                   RETURNING id"""
            ),
            {
                "email": DEFAULT_TEST_CLIENT_EMAIL,
                "password_hash": get_password_hash("not-used-in-tests"),
                "role": "client",
            },
        )
        await conn.execute(text("SET search_path TO public"))

    yield {
        "tenant_id": tenant_ids[DEFAULT_TEST_TENANT_SLUG],
        "tenant_slug": DEFAULT_TEST_TENANT_SLUG,
        "default_tenant_id": tenant_ids[DEFAULT_INTEGRATION_TENANT_SLUG],
        "default_tenant_slug": DEFAULT_INTEGRATION_TENANT_SLUG,
        "legacy_tenant_id": tenant_ids[LEGACY_TEST_TENANT_SLUG],
        "legacy_tenant_slug": LEGACY_TEST_TENANT_SLUG,
        "user_id": demo_admin_user_id,
        "staff_user_id": demo_staff_user_id,
        "client_user_id": demo_client_user_id,
    }


@pytest.fixture
async def db_session(db_engine, bootstrap_default_tenant):
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
        await conn.execute(text(f'SET search_path TO "{tenant_schema_name(DEFAULT_TEST_TENANT_SLUG)}", public'))
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


@pytest.fixture
async def public_session(db_session):
    """Alias retrocompatible de ``db_session`` pour les tests du schema public.

    Yields:
        La meme session isolee que ``db_session``.
    """
    await db_session.execute(text("SET search_path TO public"))
    yield db_session


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()


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
def demo_tenant_slug(bootstrap_default_tenant) -> str:
    return bootstrap_default_tenant["tenant_slug"]


@pytest.fixture
async def authed_client(bootstrap_default_tenant):
    token = create_access_token(
        {
            "sub": str(bootstrap_default_tenant["user_id"]),
            "email": DEFAULT_TEST_ADMIN_EMAIL,
            "role": "admin",
            "tenant_id": bootstrap_default_tenant["tenant_id"],
            "tenant_slug": bootstrap_default_tenant["tenant_slug"],
            "permissions": None,
            "must_change_password": False,
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as test_client:
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
