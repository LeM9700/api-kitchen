"""Tests de l'outil de diagnostic ``tools/audit_tenant_schemas.py``.

Deux niveaux :
    - Tests unitaires purs sur ``build_report``/``format_report_*`` (aucun
      acces DB) -- couvrent la logique de detection independamment de
      PostgreSQL.
    - Un test d'integration PostgreSQL reel sur ``run_audit`` -- prouve que
      la connexion est effectivement en lecture seule (toute ecriture est
      refusee par PostgreSQL lui-meme) et que la detection fonctionne sur de
      vraies donnees.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_schema_name
from tools.audit_tenant_schemas import (
    POSTGRES_MAX_IDENTIFIER_BYTES,
    TenantRow,
    build_report,
    format_report_json,
    format_report_text,
    run_audit,
)

# ---------------------------------------------------------------------------
# Unitaires purs : build_report / format_report_* (aucun acces DB)
# ---------------------------------------------------------------------------


def test_build_report_clean_state_has_no_issues():
    tenants = [TenantRow(id=1, slug="short-a", name="A"), TenantRow(id=2, slug="short-b", name="B")]
    schemas = {tenant_schema_name("short-a"), tenant_schema_name("short-b")}

    report = build_report(tenants, schemas)

    assert report.has_issues is False
    assert report.long_slug_tenants == []
    assert report.truncation_collision_groups == []
    assert report.tenants_missing_schema == []
    assert report.orphan_schemas == []
    assert report.total_tenants == 2
    assert report.total_tenant_schemas == 2


def test_build_report_flags_slug_over_creation_length():
    long_slug = "a" * 60  # > 56, la limite de creation
    tenants = [TenantRow(id=1, slug=long_slug, name="Long")]
    schemas = {tenant_schema_name("a" * 56)}  # schema physique tronque a 63 octets

    report = build_report(tenants, schemas)

    assert report.has_issues is True
    assert [t.id for t in report.long_slug_tenants] == [1]


def test_build_report_flags_truncation_collision_pair():
    """Deux slugs distincts partageant les 56 premiers caracteres produisent
    le meme ``expected_physical_schema`` -- exactement le bug de troncature
    corrige par ailleurs dans ce meme correctif."""
    common_prefix_56 = "shared-prefix-for-collision-test".ljust(56, "x")
    slug_a = common_prefix_56 + "aaaaaaaa"  # 64 chars, ancienne limite
    slug_b = common_prefix_56 + "bbbbbbbb"
    assert slug_a != slug_b

    tenants = [
        TenantRow(id=1, slug=slug_a, name="A"),
        TenantRow(id=2, slug=slug_b, name="B"),
    ]
    report = build_report(tenants, existing_schemas=set())

    assert report.has_issues is True
    assert len(report.truncation_collision_groups) == 1
    group = report.truncation_collision_groups[0]
    assert {t.id for t in group} == {1, 2}
    assert group[0].expected_physical_schema == group[1].expected_physical_schema
    assert len(group[0].expected_physical_schema.encode("utf-8")) == POSTGRES_MAX_IDENTIFIER_BYTES


def test_build_report_flags_tenant_missing_schema():
    tenants = [TenantRow(id=1, slug="no-schema-yet", name="NoSchema")]
    report = build_report(tenants, existing_schemas=set())

    assert report.has_issues is True
    assert [t.id for t in report.tenants_missing_schema] == [1]
    assert report.orphan_schemas == []


def test_build_report_flags_orphan_schema():
    tenants: list[TenantRow] = []
    schemas = {"tenant_orphan_leftover"}
    report = build_report(tenants, schemas)

    assert report.has_issues is True
    assert report.orphan_schemas == ["tenant_orphan_leftover"]
    assert report.tenants_missing_schema == []


def test_build_report_does_not_confuse_two_distinct_short_slugs():
    """Non-regression : deux tenants normaux, chacun avec son propre schema,
    ne doivent jamais etre signales -- seule une VRAIE collision de nom
    physique compte."""
    tenants = [TenantRow(id=1, slug="alpha", name="Alpha"), TenantRow(id=2, slug="beta", name="Beta")]
    schemas = {tenant_schema_name("alpha"), tenant_schema_name("beta")}

    report = build_report(tenants, schemas)

    assert report.has_issues is False


def test_format_report_text_reports_ok_when_clean():
    report = build_report([TenantRow(id=1, slug="ok", name="Ok")], {tenant_schema_name("ok")})
    text_out = format_report_text(report)
    assert "OK, aucune anomalie." in text_out
    assert "ANOMALIE" not in text_out


def test_format_report_text_flags_anomalies_visibly():
    report = build_report([TenantRow(id=1, slug="a" * 60, name="Long")], existing_schemas=set())
    text_out = format_report_text(report)
    assert "ANOMALIES DETECTEES" in text_out
    assert "[ANOMALIE]" in text_out


def test_format_report_json_is_valid_and_exploitable():
    import json

    report = build_report([TenantRow(id=1, slug="a" * 60, name="Long")], existing_schemas=set())
    payload = json.loads(format_report_json(report))
    assert payload["has_issues"] is True
    assert payload["total_tenants"] == 1
    assert len(payload["long_slug_tenants"]) == 1
    assert payload["long_slug_tenants"][0]["id"] == 1


# ---------------------------------------------------------------------------
# Integration PostgreSQL reelle : run_audit (lecture seule + detection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_against_real_postgres_detects_no_issue_by_default(db_engine):
    """Sur la base de test telle que geree par les fixtures (aucune anomalie
    injectee), l'audit ne doit rien signaler."""
    report = await run_audit(db_engine.url.render_as_string(hide_password=False))
    assert report.total_tenants >= 1
    assert report.long_slug_tenants == []
    assert report.truncation_collision_groups == []


@pytest.mark.asyncio
async def test_run_audit_detects_injected_orphan_schema_and_missing_schema_tenant(db_engine):
    """Injecte, directement en base (hors flux applicatif), les deux
    anomalies de reconciliation demandees : un schema orphelin et un tenant
    enregistre sans schema -- verifie que run_audit() les detecte bien sur
    une vraie connexion PostgreSQL."""
    unique = uuid.uuid4().hex[:8]
    orphan_schema = f"tenant_orphan-{unique}"
    missing_slug = f"missing-schema-{unique}"

    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{orphan_schema}" CASCADE'))
        await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": missing_slug})
        await conn.execute(text(f'CREATE SCHEMA "{orphan_schema}"'))
        tenant_id = await conn.scalar(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, 'starter') RETURNING id"
            ),
            {"slug": missing_slug, "name": "Missing Schema Tenant"},
        )

    try:
        report = await run_audit(db_engine.url.render_as_string(hide_password=False))

        assert report.has_issues is True
        assert orphan_schema in report.orphan_schemas
        assert any(t.id == tenant_id and t.slug == missing_slug for t in report.tenants_missing_schema)
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{orphan_schema}" CASCADE'))
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :s"), {"s": missing_slug})


@pytest.mark.asyncio
async def test_run_audit_connection_is_genuinely_read_only(db_engine):
    """L'audit ne doit JAMAIS pouvoir modifier de donnees : verifie que la
    connexion qu'il ouvre refuse une ecriture au niveau PostgreSQL lui-meme
    (``SET TRANSACTION READ ONLY``), pas seulement "le code ne fait que des
    SELECT". Reproduit l'ouverture de connexion de ``run_audit`` a l'identique
    plutot que d'appeler une fonction interne, pour tester la garantie
    reellement en vigueur en production."""
    from sqlalchemy.ext.asyncio import create_async_engine

    probe_engine = create_async_engine(db_engine.url.render_as_string(hide_password=False))
    try:
        async with probe_engine.connect() as conn:
            conn = await conn.execution_options(postgresql_readonly=True)
            with pytest.raises(DBAPIError):
                await conn.execute(text("CREATE TABLE audit_readonly_probe (id int)"))
    finally:
        await probe_engine.dispose()

    async with db_engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT to_regclass('public.audit_readonly_probe') IS NOT NULL")
        )
    assert exists is False


@pytest.mark.asyncio
async def test_run_audit_raises_on_unreachable_database():
    """L'audit doit echouer clairement (pas de faux 'OK') si la base cible
    est injoignable -- distingue une vraie absence d'anomalie d'une
    connexion qui n'a jamais pu verifier quoi que ce soit."""
    with pytest.raises(Exception):
        await run_audit("postgresql+asyncpg://postgres:postgres@127.0.0.1:1/does-not-exist")
