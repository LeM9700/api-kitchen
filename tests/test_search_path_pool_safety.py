"""Fiabilite du contexte PostgreSQL tenant sous pool de connexions (Prompt 03).

Contexte : ``SET search_path`` (sans LOCAL) modifie l'etat d'une connexion
PostgreSQL PHYSIQUE. Verifie empiriquement (voir
app/core/database/session.py, commentaire en tete de fichier) :

    - un ``SET`` PERSISTE apres COMMIT, y compris apres retour au pool et
      reutilisation par un tout autre appelant ;
    - un ``ROLLBACK`` ne l'annule qu'en revenant a la valeur d'AVANT la
      transaction -- qui peut elle-meme etre un residu tenant ;
    - ``AsyncSession.commit()`` LIBERE la connexion sous-jacente au pool ; la
      requete suivante sur la MEME session re-emprunte une connexion (souvent
      la meme physiquement, mais passee par le cycle checkin/checkout du pool
      entre-temps) -- un ``SET`` unique en tete de session ne survit donc pas
      forcement a un commit interne.

Ce fichier verifie, contre un PostgreSQL reel et un pool de connexions
REELLEMENT reutilisable (``pool_size=1`` pour forcer une reutilisation
deterministe de la meme connexion physique, plus des assertions sur
``pg_backend_pid()`` qui le prouvent plutot que de le supposer), que la
strategie a deux niveaux de ``app/core/database/session.py``
(``SessionEvents.after_begin`` + ``PoolEvents.reset``) empeche toute
confusion de contexte entre tenant A, tenant B et public -- quelle que soit
la maniere dont l'usage precedent d'une connexion s'est termine.
"""

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import get_public_session, get_tenant_session, tenant_schema_name
from app.core.database.session import _SEARCH_PATH_SESSION_INFO_KEY, _reset_search_path_on_checkin

TENANT_A_SLUG = "test"
TENANT_B_SLUG = "default"
PUBLIC_MARKER = "PUBLIC_MARKER"
TENANT_A_MARKER = "TENANT_A_MARKER"
TENANT_B_MARKER = "TENANT_B_MARKER"


@pytest.fixture
async def probe_tables(db_engine):
    """Cree une table ``search_path_probe`` de MEME NOM dans ``public`` ET
    dans les deux schemas tenant de test, chacune avec un marqueur distinct.

    Une session dont le search_path est correct ne peut lire QUE le marqueur
    de son propre schema -- toute fuite (repli vers public, ou vers l'autre
    tenant) se traduit par un marqueur inattendu, pas par une erreur muette.
    """
    schema_a = tenant_schema_name(TENANT_A_SLUG)
    schema_b = tenant_schema_name(TENANT_B_SLUG)

    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS public.search_path_probe"))
        await conn.execute(text("CREATE TABLE public.search_path_probe (marker text)"))
        await conn.execute(
            text("INSERT INTO public.search_path_probe (marker) VALUES (:m)"), {"m": PUBLIC_MARKER}
        )
        for schema, marker in ((schema_a, TENANT_A_MARKER), (schema_b, TENANT_B_MARKER)):
            await conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".search_path_probe'))
            await conn.execute(text(f'CREATE TABLE "{schema}".search_path_probe (marker text)'))
            await conn.execute(
                text(f'INSERT INTO "{schema}".search_path_probe (marker) VALUES (:m)'), {"m": marker}
            )

    yield

    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS public.search_path_probe"))
        await conn.execute(text(f'DROP TABLE IF EXISTS "{schema_a}".search_path_probe'))
        await conn.execute(text(f'DROP TABLE IF EXISTS "{schema_b}".search_path_probe'))


async def _read_marker(session) -> str:
    return await session.scalar(text("SELECT marker FROM search_path_probe"))


async def _backend_pid(session) -> int:
    return await session.scalar(text("SELECT pg_backend_pid()"))


# ---------------------------------------------------------------------------
# 1. Le vrai point d'entree applicatif (get_tenant_session/get_public_session,
#    moteur de production) ne confond jamais A, B et public.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_session_never_reads_public_probe(probe_tables):
    async with get_tenant_session(TENANT_A_SLUG) as session:
        assert await _read_marker(session) == TENANT_A_MARKER


@pytest.mark.asyncio
async def test_public_session_never_reads_tenant_probe(probe_tables):
    async with get_public_session() as session:
        assert await _read_marker(session) == PUBLIC_MARKER


@pytest.mark.asyncio
async def test_tenant_a_commit_then_reuse_then_tenant_b(probe_tables):
    async with get_tenant_session(TENANT_A_SLUG) as session:
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()

    async with get_tenant_session(TENANT_B_SLUG) as session:
        assert await _read_marker(session) == TENANT_B_MARKER


@pytest.mark.asyncio
async def test_tenant_a_exception_then_rollback_then_tenant_b(probe_tables):
    with pytest.raises(RuntimeError):
        async with get_tenant_session(TENANT_A_SLUG) as session:
            assert await _read_marker(session) == TENANT_A_MARKER
            raise RuntimeError("simulated failure mid-request")

    async with get_tenant_session(TENANT_B_SLUG) as session:
        assert await _read_marker(session) == TENANT_B_MARKER


@pytest.mark.asyncio
async def test_tenant_a_then_pool_reuse_then_public_session(probe_tables):
    async with get_tenant_session(TENANT_A_SLUG) as session:
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()

    async with get_public_session() as session:
        assert await _read_marker(session) == PUBLIC_MARKER


@pytest.mark.asyncio
async def test_public_session_then_tenant_a(probe_tables):
    async with get_public_session() as session:
        assert await _read_marker(session) == PUBLIC_MARKER

    async with get_tenant_session(TENANT_A_SLUG) as session:
        assert await _read_marker(session) == TENANT_A_MARKER


@pytest.mark.asyncio
async def test_alternating_a_b_a(probe_tables):
    for expected_slug, expected_marker in (
        (TENANT_A_SLUG, TENANT_A_MARKER),
        (TENANT_B_SLUG, TENANT_B_MARKER),
        (TENANT_A_SLUG, TENANT_A_MARKER),
    ):
        async with get_tenant_session(expected_slug) as session:
            assert await _read_marker(session) == expected_marker
            await session.commit()


@pytest.mark.asyncio
async def test_internal_commit_followed_by_another_query_same_session(probe_tables):
    """Reproduit exactement le pattern qui a fait echouer une premiere
    version de ce correctif (voir app/modules/favorites/router.py::add_favorite
    -- commit() puis refresh() sur la MEME session) : un commit interne ne
    doit JAMAIS faire perdre le search_path pour la requete suivante de la
    meme session logique."""
    async with get_tenant_session(TENANT_A_SLUG) as session:
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()
        # Nouvelle requete sur LA MEME session, apres le commit interne --
        # doit toujours voir le schema tenant A, pas public ni un residu.
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.execute(text("SELECT 1"))
        await session.commit()
        assert await _read_marker(session) == TENANT_A_MARKER


# ---------------------------------------------------------------------------
# 2. Preuve deterministe de reutilisation de connexion (pool_size=1) : les
#    memes scenarios, mais avec verification explicite (pg_backend_pid) que
#    c'est bien la MEME connexion physique qui est reutilisee -- pas une
#    coincidence de pool avec de la place libre.
# ---------------------------------------------------------------------------


@pytest.fixture
async def single_connection_setup(probe_tables):
    """Moteur dedie avec un pool a UNE seule connexion (``pool_size=1,
    max_overflow=0``) : toute sequence non concurrente de checkouts est
    GARANTIE de reutiliser la meme connexion physique, rendant les tests
    ci-dessous deterministes plutot que dependants de l'etat du pool partage
    par le reste de la suite.

    Reenregistre ``_reset_search_path_on_checkin`` -- la VRAIE fonction de
    production, importee depuis app/core/database/session.py, pas une copie
    -- sur ce moteur dedie (les evenements de pool sont par-moteur).
    ``SessionEvents.after_begin`` n'a pas besoin d'un nouvel enregistrement :
    il est attache a la classe ``Session`` de base et s'applique donc
    automatiquement a toute session, y compris celles de ce moteur.
    """
    database_url = settings.test_database_url or settings.database_url
    test_engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    event.listen(test_engine.sync_engine, "reset", _reset_search_path_on_checkin)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await test_engine.dispose()


def _session_cm(factory, search_path: str | None):
    """Reproduit exactement get_tenant_session()/get_public_session() (meme
    cle session.info, meme mecanisme) mais liee au moteur de test dedie."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        async with factory() as session:
            if search_path is not None:
                session.info[_SEARCH_PATH_SESSION_INFO_KEY] = search_path
            yield session

    return _cm()


@pytest.mark.asyncio
async def test_single_connection_reuse_is_real(single_connection_setup):
    """Verifie la premisse des tests suivants : le pool a une seule
    connexion rend bien la MEME connexion physique a chaque checkout."""
    factory = single_connection_setup
    async with _session_cm(factory, None) as session:
        pid1 = await _backend_pid(session)
    async with _session_cm(factory, None) as session:
        pid2 = await _backend_pid(session)
    assert pid1 == pid2


@pytest.mark.asyncio
async def test_single_connection_tenant_a_commit_then_tenant_b(single_connection_setup):
    factory = single_connection_setup
    schema_a = f'"{tenant_schema_name(TENANT_A_SLUG)}"'
    schema_b = f'"{tenant_schema_name(TENANT_B_SLUG)}"'

    async with _session_cm(factory, schema_a) as session:
        pid_a = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()

    async with _session_cm(factory, schema_b) as session:
        pid_b = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_B_MARKER

    assert pid_a == pid_b, "ce test suppose la reutilisation de la meme connexion physique"


@pytest.mark.asyncio
async def test_single_connection_tenant_a_rollback_then_tenant_b(single_connection_setup):
    factory = single_connection_setup
    schema_a = f'"{tenant_schema_name(TENANT_A_SLUG)}"'
    schema_b = f'"{tenant_schema_name(TENANT_B_SLUG)}"'

    with pytest.raises(RuntimeError):
        async with _session_cm(factory, schema_a) as session:
            pid_a = await _backend_pid(session)
            assert await _read_marker(session) == TENANT_A_MARKER
            raise RuntimeError("boom")

    async with _session_cm(factory, schema_b) as session:
        pid_b = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_B_MARKER

    assert pid_a == pid_b, "ce test suppose la reutilisation de la meme connexion physique"


@pytest.mark.asyncio
async def test_single_connection_tenant_a_then_public_session(single_connection_setup):
    factory = single_connection_setup
    schema_a = f'"{tenant_schema_name(TENANT_A_SLUG)}"'

    async with _session_cm(factory, schema_a) as session:
        pid_a = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()

    # Session "publique" -- ne fixe AUCUN search_path, exactement comme un
    # appelant qui aurait oublie de configurer session.info. Ne doit compter
    # QUE sur le filet de securite du pool (_reset_search_path_on_checkin).
    async with _session_cm(factory, None) as session:
        pid_none = await _backend_pid(session)
        assert await _read_marker(session) == PUBLIC_MARKER

    assert pid_a == pid_none, "ce test suppose la reutilisation de la meme connexion physique"


@pytest.mark.asyncio
async def test_single_connection_alternating_a_b_a(single_connection_setup):
    factory = single_connection_setup
    schema_a = f'"{tenant_schema_name(TENANT_A_SLUG)}"'
    schema_b = f'"{tenant_schema_name(TENANT_B_SLUG)}"'

    pids = []
    for schema, expected_marker in (
        (schema_a, TENANT_A_MARKER),
        (schema_b, TENANT_B_MARKER),
        (schema_a, TENANT_A_MARKER),
    ):
        async with _session_cm(factory, schema) as session:
            pids.append(await _backend_pid(session))
            assert await _read_marker(session) == expected_marker
            await session.commit()

    assert len(set(pids)) == 1, "ce test suppose la reutilisation de la meme connexion physique"


@pytest.mark.asyncio
async def test_single_connection_internal_commit_then_more_queries(single_connection_setup):
    """Meme scenario que test_internal_commit_followed_by_another_query_same_session
    mais avec preuve explicite (pid) que c'est la meme connexion physique qui
    traverse le commit interne."""
    factory = single_connection_setup
    schema_a = f'"{tenant_schema_name(TENANT_A_SLUG)}"'

    async with _session_cm(factory, schema_a) as session:
        pid_before = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_A_MARKER
        await session.commit()
        pid_after = await _backend_pid(session)
        assert await _read_marker(session) == TENANT_A_MARKER

    assert pid_before == pid_after
