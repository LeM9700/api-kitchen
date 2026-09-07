"""Isolation multi-tenant : collision de noms de schema PostgreSQL par troncature.

Contexte (Prompt 01, priorite critique) :
    Le slug tenant etait accepte jusqu'a 64 caracteres puis prefixe par
    ``tenant_`` pour former le nom physique du schema. PostgreSQL tronque
    silencieusement tout identifiant de plus de 63 octets (NAMEDATALEN=64)
    au lieu de rejeter la requete : deux slugs distincts de 64 caracteres
    partageant les 56 premiers caracteres produisaient donc EXACTEMENT le
    meme nom de schema tronque -- un tenant pouvait alors se retrouver a
    utiliser (silencieusement, via ``CREATE SCHEMA IF NOT EXISTS``) le
    schema, donc les donnees, d'un autre tenant.

Correctif (voir app/core/database/session.py, app/core/tenancy/tenant.py) :
    - Tout NOUVEAU tenant est borne a 56 caracteres de slug (56 + len("tenant_")
      == 63 == la limite PostgreSQL), ce qui rend la troncature structurellement
      impossible plutot que simplement improbable. Applique aux DEUX parcours de
      creation (inscription standard ET creation super-admin).
    - Le contrat de RESOLUTION d'un schema tenant EXISTANT (``tenant_schema_name``,
      utilise a chaque requete) reste volontairement inchange (jusqu'a 64
      caracteres) pour ne jamais casser l'acces a un tenant deja provisionne
      avant ce correctif.
    - ``create_tenant_schema`` n'utilise plus ``CREATE SCHEMA IF NOT EXISTS`` :
      un schema deja present sous ce nom (collision, residu, creation
      concurrente) fait desormais echouer la creation avec une erreur
      explicite plutot que d'etre reutilise en silence.
"""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import text

from app.core.auth.security import compute_token_lookup, create_access_token
from app.core.database import (
    NEW_TENANT_SLUG_RE,
    TENANT_SLUG_MAX_LENGTH_FOR_CREATION,
    TENANT_SLUG_RE,
    get_public_session,
    tenant_schema_name,
)
from app.core.http.errors import AppError
from app.core.tenancy.tenant import create_tenant_schema
from app.modules.admin.tenants.lifecycle_router import TenantCreate
from app.modules.auth.schemas import RegisterRequest

POSTGRES_MAX_IDENTIFIER_LENGTH = 63


async def _create_super_admin(email: str) -> int:
    """Insere un vrai compte public.super_admins (meme pattern que
    tests/test_super_admin_auth.py) pour authentifier les tests du parcours
    de creation de tenant super-admin."""
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


# ---------------------------------------------------------------------------
# 1. La longueur maximale de creation rend la troncature PostgreSQL impossible
# ---------------------------------------------------------------------------


def test_new_tenant_slug_max_length_keeps_schema_name_within_postgres_limit():
    """"tenant_" + le slug le plus long autorise a la creation == exactement 63
    octets, la limite d'identifiant PostgreSQL -- jamais au-dela."""
    longest_allowed_slug = "a" * TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    schema = tenant_schema_name(longest_allowed_slug)
    assert len(schema.encode("utf-8")) == POSTGRES_MAX_IDENTIFIER_LENGTH


def test_new_tenant_slug_regex_rejects_slug_one_character_too_long():
    too_long_slug = "a" * (TENANT_SLUG_MAX_LENGTH_FOR_CREATION + 1)
    assert not NEW_TENANT_SLUG_RE.fullmatch(too_long_slug)
    # ... mais la regle de RESOLUTION historique (tenants deja existants) l'accepte
    # encore : ce n'est qu'a la CREATION qu'on interdit desormais ce slug.
    assert TENANT_SLUG_RE.fullmatch(too_long_slug)


def test_new_tenant_slug_regex_accepts_slug_at_exact_max_length():
    max_length_slug = "a" * TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    assert NEW_TENANT_SLUG_RE.fullmatch(max_length_slug)


def test_two_distinct_slugs_that_previously_collided_are_both_rejected_at_creation():
    """Avant le correctif, deux slugs de 64 caracteres partageant les 56
    premiers caracteres produisaient le meme schema tronque a 63 octets.
    Les deux doivent maintenant etre rejetes AVANT toute tentative de
    creation de schema -- aucun des deux ne doit pouvoir passer la
    validation Pydantic du parcours d'inscription."""
    # Les 56 premiers caracteres (= la nouvelle limite de creation) sont
    # STRICTEMENT identiques entre les deux slugs : c'est precisement ce
    # prefixe commun qu'une troncature PostgreSQL a 63 octets aurait retenu.
    common_prefix_56 = "collision-prone-tenant-slug-shared-prefix".ljust(56, "x")
    assert len(common_prefix_56) == 56

    # Etend chaque slug jusqu'a l'ancienne limite de 64 caracteres (autorisee
    # avant ce correctif) : sous l'ancienne regle, "tenant_" + ces 64 caracteres
    # (71 octets) aurait ete tronque par PostgreSQL a 63 octets, soit exactement
    # "tenant_" + ``common_prefix_56`` -- IDENTIQUE pour les deux. Verifie que la
    # nouvelle regle de creation rejette desormais les deux avant que ce
    # scenario puisse se produire.
    old_rule_slug_a = common_prefix_56 + "aaaaaaaa"  # 64 chars, diverge seulement apres le char 56
    old_rule_slug_b = common_prefix_56 + "bbbbbbbb"  # 64 chars
    assert len(old_rule_slug_a) == len(old_rule_slug_b) == 64
    assert old_rule_slug_a != old_rule_slug_b
    assert old_rule_slug_a[:56] == old_rule_slug_b[:56] == common_prefix_56

    assert not NEW_TENANT_SLUG_RE.fullmatch(old_rule_slug_a)
    assert not NEW_TENANT_SLUG_RE.fullmatch(old_rule_slug_b)

    with pytest.raises(ValueError):
        RegisterRequest(
            tenant_slug=old_rule_slug_a,
            tenant_name="Collision A",
            email="a@collision-test.com",
            password="Valid1!aa",
        )
    with pytest.raises(ValueError):
        RegisterRequest(
            tenant_slug=old_rule_slug_b,
            tenant_name="Collision B",
            email="b@collision-test.com",
            password="Valid1!aa",
        )


# ---------------------------------------------------------------------------
# 2. Les DEUX parcours de creation appliquent la meme regle stricte
# ---------------------------------------------------------------------------


def test_register_request_rejects_slug_over_creation_max_length():
    too_long_slug = "a" * (TENANT_SLUG_MAX_LENGTH_FOR_CREATION + 1)
    with pytest.raises(ValueError):
        RegisterRequest(
            tenant_slug=too_long_slug,
            tenant_name="Test",
            email="test@example.com",
            password="Valid1!aa",
        )


def test_register_request_accepts_slug_at_exact_creation_max_length():
    max_length_slug = "a" * TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    body = RegisterRequest(
        tenant_slug=max_length_slug,
        tenant_name="Test",
        email="test@example.com",
        password="Valid1!aa",
    )
    assert body.tenant_slug == max_length_slug


def test_super_admin_tenant_create_rejects_slug_over_creation_max_length():
    """Le parcours super-admin (lifecycle_router.TenantCreate) n'imposait
    AUCUNE limite de longueur/forme sur ``slug`` avant ce correctif -- il
    doit desormais appliquer exactement la meme regle que l'inscription
    standard."""
    too_long_slug = "a" * (TENANT_SLUG_MAX_LENGTH_FOR_CREATION + 1)
    with pytest.raises(ValueError):
        TenantCreate(
            slug=too_long_slug,
            name="Test",
            admin_email="admin@example.com",
        )


def test_super_admin_tenant_create_rejects_invalid_characters():
    with pytest.raises(ValueError):
        TenantCreate(
            slug="Not A Valid Slug!!",
            name="Test",
            admin_email="admin@example.com",
        )


def test_super_admin_tenant_create_accepts_slug_at_exact_creation_max_length():
    max_length_slug = "a" * TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    body = TenantCreate(
        slug=max_length_slug,
        name="Test",
        admin_email="admin@example.com",
    )
    assert body.slug == max_length_slug


# ---------------------------------------------------------------------------
# 3. Resolution d'un tenant EXISTANT : contrat inchange (retro-compatibilite)
# ---------------------------------------------------------------------------


def test_existing_tenant_lookup_still_accepts_historical_slug_length():
    """La regle de RESOLUTION (tenant_schema_name / get_tenant_session), utilisee
    a chaque requete pour un tenant qui existe deja, ne doit pas devenir plus
    stricte : un tenant provisionne avant ce correctif (notamment via le
    parcours super-admin, qui ne bornait pas du tout la longueur avant ce fix)
    avec un slug de plus de 56 caracteres doit rester accessible."""
    legacy_slug = "a" * 60  # > 56 (nouvelle limite creation), <= 64 (ancienne limite)
    assert TENANT_SLUG_RE.fullmatch(legacy_slug)
    # Ne leve pas -- la resolution reste permissive pour les tenants existants.
    schema = tenant_schema_name(legacy_slug)
    assert schema == f"tenant_{legacy_slug}"


# ---------------------------------------------------------------------------
# 4. create_tenant_schema : plus de reutilisation silencieuse d'un schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_schema_produces_exact_non_truncated_schema_name(db_engine):
    """Un slug a la longueur maximale de creation produit, en base, un schema
    dont le nom n'est PAS tronque -- preuve directe (pas seulement au niveau
    Python) que la collision par troncature est devenue impossible."""
    slug = "z" * TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    schema = tenant_schema_name(slug)

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    try:
        await create_tenant_schema(slug)

        async with db_engine.connect() as conn:
            nspname = await conn.scalar(
                text("SELECT nspname FROM pg_namespace WHERE nspname = :s"),
                {"s": schema},
            )
        assert nspname == schema
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.asyncio
async def test_create_tenant_schema_raises_explicit_error_on_existing_schema(db_engine):
    """Un schema deja present sous le nom cible (appartenant potentiellement a
    un AUTRE tenant) doit faire echouer la creation explicitement -- jamais
    etre reutilise en silence via ``CREATE SCHEMA IF NOT EXISTS``."""
    slug = f"collision-{uuid.uuid4().hex[:8]}"
    schema = tenant_schema_name(slug)

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        # Simule un schema deja existant sous ce nom (collision, ou residu
        # d'un tenant precedent) -- pas de tables dedans : si create_tenant_schema
        # reutilisait ce schema en silence, ce test le detecterait uniquement
        # via l'absence d'erreur, ce qui est precisement ce qu'on interdit ici.
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    try:
        with pytest.raises(AppError) as exc_info:
            await create_tenant_schema(slug)
        assert exc_info.value.code == "TENANT_SCHEMA_COLLISION"
        assert exc_info.value.status_code == 409
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# ---------------------------------------------------------------------------
# 4bis. Bout-en-bout (vrai endpoint HTTP + PostgreSQL reel) : pas de ligne
#       orpheline, pas d'impact sur le tenant A deja proprietaire du schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_endpoint_collision_leaves_no_orphan_row_and_does_not_touch_tenant_a(
    client, db_engine
):
    """Reproduction bout-en-bout (vrai endpoint HTTP ``/auth/register``,
    PostgreSQL reel) du scenario :

        1. Le tenant A possede deja un schema physique qui entre en collision
           avec le nom calcule pour un nouveau tenant B.
        2. La creation de B echoue avec TENANT_SCHEMA_COLLISION.
        3. Aucune ligne orpheline ne reste dans public.tenants pour B.
        4. Aucune donnee ni schema existant de A n'est modifie.

    A est represente comme un tenant PLEINEMENT FONCTIONNEL (ligne
    public.tenants + schema provisionne avec de vraies tables/donnees), dont
    le schema physique ne correspond pas au nom que son slug logique
    produirait naivement -- exactement le type d'anomalie (residu
    historique, schema renomme manuellement, incident ops...) que
    ``tools/audit_tenant_schemas.py`` est concu pour detecter. B tente de
    s'inscrire avec un slug frais dont le schema calcule tombe PRECISEMENT
    sur le schema physique deja occupe par A.
    """
    from app.core.tenancy.provisioning import _provision_tenant_schema

    unique = uuid.uuid4().hex[:8]
    slug_a_logical = f"legacy-tenant-a-{unique}"
    slug_b = f"fresh-tenant-b-{unique}"
    # Le schema physique de A ne correspond PAS a tenant_schema_name(slug_a_logical) --
    # il occupe deja, pour une raison operationnelle quelconque, exactement le nom
    # que B va calculer depuis son propre slug.
    colliding_schema = tenant_schema_name(slug_b)
    assert colliding_schema != tenant_schema_name(slug_a_logical)
    marker = f"A-DATA-MARKER-{unique}"

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{colliding_schema}" CASCADE'))
        await conn.execute(
            text("DELETE FROM public.tenants WHERE slug IN (:a, :b)"),
            {"a": slug_a_logical, "b": slug_b},
        )
        tenant_a_id = await conn.scalar(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, 'starter') RETURNING id"
            ),
            {"slug": slug_a_logical, "name": "Legacy Tenant A"},
        )
        await conn.execute(text(f'CREATE SCHEMA "{colliding_schema}"'))
        # _provision_tenant_schema(conn, slug_b) cree les tables applicatives dans
        # tenant_schema_name(slug_b) == colliding_schema -- exactement le schema
        # deja cree ci-dessus pour A. slug_b n'est utilise ici QUE pour calculer
        # le nom physique cible ; A reste identifie par slug_a_logical.
        await _provision_tenant_schema(conn, slug_b)
        await conn.execute(text(f'SET search_path TO "{colliding_schema}"'))
        await conn.execute(text("UPDATE establishments SET name = :name"), {"name": marker})
        await conn.execute(text("SET search_path TO public"))

    try:
        async with db_engine.connect() as conn:
            a_row_before = dict(
                (
                    await conn.execute(
                        text("SELECT id, slug, name, plan FROM public.tenants WHERE id = :id"),
                        {"id": tenant_a_id},
                    )
                ).mappings().one()
            )
            await conn.execute(text(f'SET search_path TO "{colliding_schema}"'))
            a_establishment_before = await conn.scalar(text("SELECT name FROM establishments LIMIT 1"))
        assert a_establishment_before == marker

        # 1 + 2. La creation de B, via le vrai endpoint HTTP, doit echouer explicitement.
        resp = await client.post("/api/v1/auth/register", json={
            "tenant_slug": slug_b,
            "tenant_name": "Fresh Tenant B",
            "email": f"b-{unique}@collision-test.com",
            "password": "Valid1!aa",
        })
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "TENANT_SCHEMA_COLLISION"

        # 3. Aucune ligne orpheline pour B.
        async with db_engine.connect() as conn:
            b_row_count = await conn.scalar(
                text("SELECT count(*) FROM public.tenants WHERE slug = :slug"),
                {"slug": slug_b},
            )
        assert b_row_count == 0

        # 4. A -- ligne public.tenants ET donnees applicatives -- est intact.
        async with db_engine.connect() as conn:
            a_row_after = dict(
                (
                    await conn.execute(
                        text("SELECT id, slug, name, plan FROM public.tenants WHERE id = :id"),
                        {"id": tenant_a_id},
                    )
                ).mappings().one()
            )
            await conn.execute(text(f'SET search_path TO "{colliding_schema}"'))
            a_establishment_after = await conn.scalar(text("SELECT name FROM establishments LIMIT 1"))
        assert a_row_after == a_row_before
        assert a_establishment_after == marker
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{colliding_schema}" CASCADE'))
            await conn.execute(
                text("DELETE FROM public.tenants WHERE slug IN (:a, :b)"),
                {"a": slug_a_logical, "b": slug_b},
            )


@pytest.mark.asyncio
async def test_super_admin_create_tenant_collision_leaves_no_orphan_row(client, db_engine):
    """Meme invariant que le test precedent, pour le SECOND parcours de
    creation (POST /admin/tenants, super-admin) -- il partage desormais le
    meme mecanisme atomique (``create_tenant_schema_on`` dans la transaction
    de l'insertion ``public.tenants``, voir lifecycle_router.create_tenant)."""
    unique = uuid.uuid4().hex[:8]
    slug_b = f"admin-fresh-b-{unique}"
    colliding_schema = tenant_schema_name(slug_b)

    admin_email = f"super-{unique}@collision-test.com"
    admin_id = await _create_super_admin(admin_email)
    token = await _super_admin_token(admin_id, admin_email)

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{colliding_schema}" CASCADE'))
        await conn.execute(text("DELETE FROM public.tenants WHERE slug = :b"), {"b": slug_b})
        # Un schema deja present sous ce nom, sans lien avec le tenant B qu'on
        # s'apprete a creer -- residu/collision, pas de ligne public.tenants dediee.
        await conn.execute(text(f'CREATE SCHEMA "{colliding_schema}"'))

    try:
        resp = await client.post(
            "/api/v1/admin/tenants",
            json={
                "slug": slug_b,
                "name": "Admin Fresh B",
                "admin_email": f"admin-{unique}@collision-test.com",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "TENANT_SCHEMA_COLLISION"

        async with db_engine.connect() as conn:
            b_row_count = await conn.scalar(
                text("SELECT count(*) FROM public.tenants WHERE slug = :slug"),
                {"slug": slug_b},
            )
        assert b_row_count == 0
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{colliding_schema}" CASCADE'))
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :b"), {"b": slug_b})
            await conn.execute(text("DELETE FROM public.super_admins WHERE id = :id"), {"id": admin_id})


# ---------------------------------------------------------------------------
# 5. Bout-en-bout : les tenants (nouveaux comme existants) restent accessibles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_endpoint_rejects_slug_too_long_with_no_partial_state(client, db_engine):
    """Un slug trop long est rejete a la validation HTTP (422), AVANT toute
    ecriture en base -- pas de ligne ``public.tenants`` orpheline."""
    too_long_slug = "b" * (TENANT_SLUG_MAX_LENGTH_FOR_CREATION + 1)
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": too_long_slug,
        "tenant_name": "Too Long",
        "email": f"toolong-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Valid1!aa",
    })
    assert resp.status_code == 422

    async with db_engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM public.tenants WHERE slug = :slug"),
            {"slug": too_long_slug},
        )
    assert count == 0


@pytest.mark.asyncio
async def test_register_endpoint_still_works_for_ordinary_slugs(client, unique_slug):
    """Non-regression : le flux d'inscription standard, pour un slug de
    longueur ordinaire (tous les tenants reels en pratique), continue de
    fonctionner de bout en bout apres le resserrement de la validation."""
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": unique_slug,
        "tenant_name": "Ordinary Tenant",
        "email": f"ordinary-{unique_slug}@test.com",
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]

    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200, me.text


@pytest.mark.asyncio
async def test_existing_bootstrap_tenants_remain_accessible(client, demo_tenant_slug):
    """Non-regression : un tenant deja provisionne avant ce correctif
    (``bootstrap_default_tenant``, cree en dehors du parcours d'inscription
    HTTP) reste pleinement accessible -- son slug n'a jamais eu besoin d'etre
    re-valide contre la nouvelle regle de creation."""
    resp = await client.post("/api/v1/auth/login", json={
        "tenant_slug": demo_tenant_slug,
        "email": "admin@test.com",
        "password": "not-used-in-tests",
    })
    assert resp.status_code == 200, resp.text
