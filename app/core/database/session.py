from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import settings
from app.core.http.errors import AppError

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,  # evite les connexions fermees cote serveur managé (Railway/Render)
    pool_pre_ping=True,
)


# [SECURITE] Strategie de contexte tenant sous pool de connexions -- lire en
# entier avant de toucher a `SET search_path` dans ce fichier. Les deux
# mecanismes ci-dessous ont ete verifies empiriquement contre PostgreSQL 16 +
# asyncpg reels (y compris un contre-exemple qui a fait echouer une premiere
# version de ce correctif -- voir le second constat).
#
# Constat 1 -- persistance de `SET` (sans LOCAL) au niveau de la connexion
# physique : dans une transaction Postgres, un `SET search_path TO x` PERSISTE
# apres COMMIT (jusqu'au prochain `SET`), y compris apres que la connexion
# soit rendue au pool puis reutilisee par un tout autre appelant. Un
# `ROLLBACK` l'annule, mais uniquement en revenant a la valeur d'AVANT cette
# transaction -- qui peut elle-meme etre un residu tenant d'un usage anterieur
# COMMITE sur cette meme connexion physique. Le pool SQLAlchemy
# (`reset_on_return`, "rollback" par defaut) ne fait qu'un ROLLBACK au retour
# en pool : il n'annule donc JAMAIS un search_path deja commite. Une session
# `get_public_session()` qui ne fixe elle-meme aucun search_path (c'etait le
# cas avant ce correctif) pouvait donc silencieusement heriter du schema
# tenant laisse par l'usage precedent de la MEME connexion physique.
#
# Constat 2 -- le Session ORM (sync ou async) NE GARDE PAS la meme transaction
# Postgres sur toute sa duree de vie : `AsyncSession.commit()` termine la
# transaction ET LIBERE la connexion sous-jacente au pool ; la prochaine
# requete sur CETTE MEME session (meme objet Python, meme schema logique
# voulu) redemarre une transaction en RE-EMPRUNTANT une connexion au pool --
# le plus souvent la meme connexion physique en l'absence de contention, mais
# passee entre-temps par le cycle checkin/checkout du pool. Verifie
# empiriquement : un `SET search_path` execute une seule fois au debut d'une
# session ne survit PAS a un `session.commit()` intermediaire si le pool a,
# entre-temps, remis cette connexion a un etat neutre (voir plus bas) -- or ce
# pattern (un service qui fait `session.commit()` puis continue d'utiliser la
# meme session, ex. `session.refresh()` apres insertion) est courant dans ce
# depot (voir `app/modules/favorites/router.py::add_favorite`). Un simple
# `SET LOCAL` unique en tete de session serait donc insuffisant pour la meme
# raison, en PIRE : `SET LOCAL` ne survit meme pas a un commit tout court.
#
# Strategie retenue (deux niveaux, complementaires, chacun couvrant l'angle
# mort de l'autre) :
#
#   1. Evenement ORM ``SessionEvents.after_begin`` (plus bas) : reapplique le
#      search_path voulu (stocke dans ``session.info``) a CHAQUE debut de
#      transaction de la session -- pas seulement la premiere. Couvre le
#      Constat 2 : que la session recommence une transaction apres un commit
#      interne ou non, le search_path correct est toujours reetabli AVANT la
#      moindre requete de cette nouvelle transaction. C'est le mecanisme
#      PRINCIPAL et suffisant a lui seul pour toute session ouverte via
#      get_tenant_session()/get_public_session().
#
#   2. Evenement pool ``PoolEvents.reset`` (plus bas) : remet la connexion a
#      ``search_path = public`` a CHAQUE retour au pool (commit, rollback OU
#      exception -- ``session.close()`` declenche systematiquement ce cycle).
#      Filet de securite structurel pour tout usage qui ne passerait PAS par
#      ``session.info`` (une connexion brute ouverte directement, un futur
#      appel qui oublierait de configurer sa session) : meme dans ce cas, la
#      connexion checked-out repart d'un etat neutre et connu plutot que d'un
#      residu tenant impossible a distinguer.
#
# Avec ces deux niveaux : une session tenant/public voit TOUJOURS son
# search_path correct au debut de CHAQUE transaction qu'elle ouvre, quel que
# soit l'usage precedent de la connexion physique sous-jacente et quelle que
# soit la maniere dont cet usage precedent s'est termine (commit, rollback,
# exception) ; et une connexion qui echappe aux deux helpers ci-dessous
# redemarre toujours sur "public" plutot que sur un schema tenant arbitraire.
_SEARCH_PATH_SESSION_INFO_KEY = "_tenant_search_path"


@event.listens_for(Session, "after_begin")
def _apply_search_path_on_transaction_begin(session, transaction, connection) -> None:
    """Reapplique le search_path de la session a CHAQUE nouvelle transaction.

    [SECURITE] ``after_begin`` se declenche a chaque "autobegin" de la
    ``Session`` -- la toute premiere transaction, ET chacune des suivantes
    apres un `commit()`/`rollback()` intermediaire sur la meme session (voir
    Constat 2 ci-dessus). C'est ce qui rend cette strategie correcte pour un
    service qui commit puis continue d'utiliser la meme session : chaque
    nouvelle transaction obtient le search_path correct AVANT sa premiere
    requete, meme si le pool a entre-temps remis la connexion a neuf.

    ``session.info`` est un dict Python ordinaire porte par la ``Session`` --
    non affecte par les commits/rollbacks Postgres. get_tenant_session() et
    get_public_session() y deposent le search_path voulu a l'ouverture ; une
    session qui n'y depose rien (ex. usage direct de ``AsyncSession(bind=...)``
    dans les tests) n'est pas touchee par cet evenement, mais reste couverte
    par le filet de securite du pool (``_reset_search_path_on_checkin``) tant
    que la connexion transite par un checkin normal.

    Args:
        session: Session ORM synchrone (l'evenement est enregistre sur la
            classe ``Session`` de base ; les objets ``AsyncSession`` de ce
            module la delegue en interne via le pont greenlet).
        transaction: Objet transaction ORM (non utilise ici).
        connection: ``Connection`` Core deja liee a cette transaction --
            executer du SQL dessus directement est le pattern documente par
            SQLAlchemy pour cet evenement.
    """
    search_path = session.info.get(_SEARCH_PATH_SESSION_INFO_KEY)
    if search_path is not None:
        connection.exec_driver_sql(f"SET search_path TO {search_path}")


@event.listens_for(engine.sync_engine, "reset")
def _reset_search_path_on_checkin(dbapi_connection, connection_record, reset_state) -> None:
    """Remet ``search_path`` a ``public`` a CHAQUE retour d'une connexion au pool.

    Filet de securite (voir strategie ci-dessus, niveau 2) : protege tout
    usage de connexion qui ne passe pas par ``session.info`` /
    ``_apply_search_path_on_transaction_begin``. Invoque par SQLAlchemy apres
    son propre reset transactionnel (rollback par defaut), donc y compris
    apres un `session.commit()` explicite, un `rollback()`, ou une exception
    qui a court-circuite les deux (`async with session:` appelle toujours
    `close()` a la sortie, qui declenche ce hook).

    [PROD] Cette API (`PoolEvents.reset`, signature a 3 arguments avec
    `reset_state`) est documentee par SQLAlchemy comme fonctionnant avec les
    moteurs asyncio : les appels DBAPI directs comme celui-ci passent par le
    pont greenlet interne, aucun `await` explicite n'est necessaire ni
    possible ici. Verifie empiriquement contre asyncpg : le `SET` doit etre
    suivi d'un `commit()` explicite sur la connexion DBAPI -- sans cela, il
    reste dans une transaction implicite non validee que le PROCHAIN checkout
    annule silencieusement (meme mecanisme de rollback-restaure-l'etat-anterieur
    que le Constat 1 ci-dessus), ce qui annulerait ce hook lui-meme.

    Args:
        dbapi_connection: Connexion DBAPI brute (adaptateur asyncpg).
        connection_record: Enregistrement interne du pool (non utilise ici).
        reset_state: Indique notamment si la connexion va etre terminee plutot
            que reutilisee (``terminate_only``) et si un event loop est
            garanti present (``asyncio_safe`` -- faux en cas de garbage
            collection, ou executer du SQL serait dangereux).
    """
    if reset_state.terminate_only or not reset_state.asyncio_safe:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET search_path TO public")
    finally:
        cursor.close()
    dbapi_connection.commit()


public_session_factory = async_sessionmaker(engine, expire_on_commit=False)

TENANT_SCHEMA_PREFIX = "tenant_"

# Contrat historique de resolution d'un schema tenant a partir d'un slug.
# Volontairement INCHANGE (forme + longueur max 64) : cette regle est utilisee
# a CHAQUE requete pour retrouver le schema d'un tenant qui existe deja
# (tenant_schema_name / get_tenant_session), y compris des tenants crees avant
# ce correctif via le parcours super-admin qui, jusqu'ici, ne bornait pas du
# tout la longueur du slug. La resserrer ici casserait l'acces a un tenant
# deja provisionne avec un slug de plus de 56 caracteres. La prevention de
# collision par troncature (voir TENANT_SLUG_MAX_LENGTH_FOR_CREATION ci-dessous)
# s'applique donc uniquement a la CREATION de nouveaux tenants, pas a la
# resolution de tenants existants.
TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")

# PostgreSQL tronque silencieusement tout identifiant > 63 octets (NAMEDATALEN=64)
# au lieu de rejeter la requete : deux slugs distincts dont les 56 premiers
# caracteres coincident produiraient donc, une fois prefixes par "tenant_" (7
# caracteres), EXACTEMENT le meme nom de schema tronque -- un tenant hériterait
# alors silencieusement des donnees d'un autre. Tout NOUVEAU tenant doit donc
# etre borne a 56 caracteres pour que "tenant_{slug}" ne puisse jamais atteindre
# cette limite, ce qui rend la troncature -- et donc la collision -- structurellement
# impossible plutot que simplement improbable. Utiliser ces constantes (et non
# TENANT_SLUG_RE) dans la validation Pydantic de TOUT parcours de creation de
# tenant (inscription standard, creation super-admin, et tout futur parcours).
POSTGRES_MAX_IDENTIFIER_LENGTH = 63
TENANT_SLUG_MAX_LENGTH_FOR_CREATION = POSTGRES_MAX_IDENTIFIER_LENGTH - len(TENANT_SCHEMA_PREFIX)
NEW_TENANT_SLUG_RE = re.compile(
    rf"^[a-z0-9][a-z0-9_-]{{0,{TENANT_SLUG_MAX_LENGTH_FOR_CREATION - 2}}}[a-z0-9]$|^[a-z0-9]$"
)


class Base(DeclarativeBase):
    pass


def tenant_schema_name(tenant_slug: str) -> str:
    if not TENANT_SLUG_RE.fullmatch(tenant_slug):
        raise AppError("INVALID_SLUG", "Invalid tenant slug", 400, "tenant_slug")
    return f"{TENANT_SCHEMA_PREFIX}{tenant_slug}"


@asynccontextmanager
async def get_public_session() -> AsyncIterator[AsyncSession]:
    """Session dont le search_path pointe EXPLICITEMENT sur ``public`` seul.

    [SECURITE] Depose ``"public"`` dans ``session.info`` : l'evenement
    ``after_begin`` (voir strategie en tete de fichier) reapplique
    ``SET search_path TO public`` a CHAQUE transaction ouverte par cette
    session, y compris apres un commit interne suivi d'une nouvelle requete
    -- pas seulement au premier statement. Le filet de securite du pool
    (``_reset_search_path_on_checkin``) couvre en plus toute connexion qui
    echapperait a ce mecanisme ; cette session ne repose donc pas sur la
    seule convention "le pool s'en occupe".
    """
    async with public_session_factory() as session:
        session.info[_SEARCH_PATH_SESSION_INFO_KEY] = "public"
        yield session


@asynccontextmanager
async def get_tenant_session(tenant_slug: str) -> AsyncIterator[AsyncSession]:
    """Session dont le search_path pointe UNIQUEMENT sur le schema du tenant.

    [SECURITE] Pas de fallback ``, public`` : ``public`` contient des tables
    historiques homonymes des tables tenant (``users``, ``orders``,
    ``products``... -- migration 0002, epoque pre-isolation-par-schema,
    jamais purgee). Avec un fallback ``, public``, une requete non qualifiee
    ciblant une table ABSENTE du schema tenant (schema incomplet suite a un
    bug de provisioning, migration tenant non encore appliquee...) ne
    leverait pas d'erreur -- elle resoudrait SILENCIEUSEMENT vers la table
    homonyme de ``public``, potentiellement partagee entre TOUS les tenants.
    C'est exactement le genre de defaut structurel que l'isolation par schema
    est censee rendre impossible (voir CLAUDE.md, section "Hidden
    constraints"). Sans fallback, la meme situation echoue explicitement
    (``UndefinedTableError``) au lieu de lire/ecrire silencieusement les
    mauvaises donnees.

    Toute table reellement globale (``public.tenants``, ``public.tenant_configs``,
    ``public.super_admins``...) doit etre referencee explicitement sous
    ``public.nom_table`` par le code applicatif -- c'est deja systematiquement
    le cas dans ce depot (voir les requetes texte de ``app/modules/auth/service.py``,
    ``app/modules/payments/service.py``...). Aucune exception a ce jour ne
    necessite un search_path multi-schema pour une session tenant.

    [SECURITE] Depose le schema dans ``session.info`` plutot que d'emettre un
    ``SET`` une seule fois : l'evenement ``after_begin`` (voir strategie en
    tete de fichier) reapplique ce search_path a CHAQUE transaction ouverte
    par cette session, y compris apres un commit interne d'un service suivi
    d'une nouvelle requete sur la MEME session (ex.
    ``app/modules/favorites/router.py::add_favorite`` :
    ``session.commit()`` puis ``session.refresh()``). Un ``SET`` unique en
    tete de session ne survivrait pas a ce genre de commit intermediaire une
    fois la connexion repassee par le cycle checkin/checkout du pool.
    """
    schema = tenant_schema_name(tenant_slug)
    async with public_session_factory() as session:
        session.info[_SEARCH_PATH_SESSION_INFO_KEY] = f'"{schema}"'
        yield session
