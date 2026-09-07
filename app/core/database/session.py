from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
    async with public_session_factory() as session:
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
    """
    schema = tenant_schema_name(tenant_slug)
    async with public_session_factory() as session:
        await session.execute(text(f'SET search_path TO "{schema}"'))
        yield session
