"""Configuration pytest et fixtures partagees.

Strategie d'isolation DB :
    Chaque test recoit une ``AsyncSession`` liee a une connexion dont la
    transaction englobante est rollbackee en teardown. La session utilise
    ``join_transaction_mode="create_savepoint"`` : chaque ``session.commit()``
    libere le savepoint courant et en ouvre un nouveau, sans jamais toucher
    la transaction englobante. Le ``conn.rollback()`` final annule tout.

    [PROD] Ce pattern ne fonctionne qu'avec PostgreSQL (SAVEPOINT) et asyncpg.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    """Boucle asyncio partagee sur toute la session de test.

    Necessaire pour que les fixtures de scope ``session`` (``db_engine``) et
    les fixtures de scope ``function`` (``db_session``) partagent la meme boucle.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


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
