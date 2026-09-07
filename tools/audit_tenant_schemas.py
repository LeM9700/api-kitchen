"""Audit en lecture seule des tenants PostgreSQL, a executer AVANT tout deploiement.

Usage:
    uv run python tools/audit_tenant_schemas.py
    uv run python tools/audit_tenant_schemas.py --database-url postgresql+asyncpg://...
    uv run python tools/audit_tenant_schemas.py --format json

Ce script ne modifie AUCUNE donnee : la connexion est ouverte en
``SET TRANSACTION READ ONLY`` (refus cote PostgreSQL de toute ecriture, pas
seulement "on n'ecrit rien depuis le code Python") et seules des requetes
``SELECT`` sont emises.

Verifie :
    1. Tenants dont le slug depasse la limite de creation (56 caracteres,
       voir app/core/database/session.py::TENANT_SLUG_MAX_LENGTH_FOR_CREATION).
    2. Couples de tenants dont ``tenant_{slug}`` partage le meme prefixe de
       63 octets -- la longueur d'identifiant que PostgreSQL retiendrait
       reellement (NAMEDATALEN=64) : deux tenants dans ce cas partagent, ou
       partageraient une fois provisionnes, EXACTEMENT le meme schema physique.
    3. Coherence entre ``public.tenants`` et les schemas ``tenant_*`` reellement
       presents dans ``pg_namespace`` -- dans les deux sens :
       a. tenants enregistres sans schema physique correspondant ;
       b. schemas ``tenant_*`` sans ligne ``public.tenants`` correspondante
          (schema orphelin).
    4. Schemas ``tenant_*`` existants mais INCOMPLETS : tables attendues
       (d'apres ``Base.metadata``, la meme source de verite que le
       provisioning -- voir app/core/tenancy/provisioning.py -- et les
       migrations Alembic) absentes du schema reel. Detecte un provisioning
       interrompu ou une migration tenant jamais appliquee a ce schema.

Sortie : code de sortie 0 si aucune anomalie, 1 si au moins une anomalie
detectee, 2 en cas d'erreur de connexion/execution -- exploitable directement
dans un pipeline de pre-deploiement (``uv run python tools/audit_tenant_schemas.py || exit 1``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import os
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base, TENANT_SCHEMA_PREFIX, TENANT_SLUG_MAX_LENGTH_FOR_CREATION
from app.core.database import tenant_models  # noqa: F401 -- peuple Base.metadata (tables tenant attendues)

POSTGRES_MAX_IDENTIFIER_BYTES = 63

# Meme source de verite que le provisioning (app/core/tenancy/provisioning.py)
# et Alembic (app/core/database/tenant_models.py) : la liste des tables
# qu'un schema tenant COMPLET doit contenir.
EXPECTED_TENANT_TABLES = frozenset(Base.metadata.tables.keys())


@dataclass(frozen=True)
class TenantRow:
    id: int
    slug: str
    name: str

    @property
    def expected_physical_schema(self) -> str:
        """Nom de schema que PostgreSQL retient REELLEMENT pour ce slug --
        y compris pour un slug invalide/trop long dont le nom naif deborde
        63 octets : PostgreSQL tronque silencieusement plutot que de rejeter,
        donc c'est CE nom tronque (pas le nom naif) qu'il faut comparer a
        ``pg_namespace`` et entre tenants pour detecter une collision reelle.
        """
        raw = f"{TENANT_SCHEMA_PREFIX}{self.slug}"
        truncated_bytes = raw.encode("utf-8")[:POSTGRES_MAX_IDENTIFIER_BYTES]
        return truncated_bytes.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class IncompleteSchema:
    schema: str
    missing_tables: list[str]


@dataclass(frozen=True)
class AuditReport:
    total_tenants: int
    total_tenant_schemas: int
    long_slug_tenants: list[TenantRow] = field(default_factory=list)
    truncation_collision_groups: list[list[TenantRow]] = field(default_factory=list)
    tenants_missing_schema: list[TenantRow] = field(default_factory=list)
    orphan_schemas: list[str] = field(default_factory=list)
    incomplete_schemas: list[IncompleteSchema] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(
            self.long_slug_tenants
            or self.truncation_collision_groups
            or self.tenants_missing_schema
            or self.orphan_schemas
            or self.incomplete_schemas
        )


def build_report(
    tenants: list[TenantRow],
    existing_schemas: set[str],
    schema_tables: dict[str, set[str]] | None = None,
) -> AuditReport:
    """Calcule le rapport d'audit a partir des donnees deja lues en base.

    Fonction pure (aucun acces DB) -- separee de ``run_audit`` pour rester
    testable unitairement sans PostgreSQL reel, en plus des tests
    d'integration qui exercent la lecture reelle.

    Args:
        tenants: Lignes ``public.tenants``.
        existing_schemas: Noms des schemas ``tenant_*`` reellement presents.
        schema_tables: Pour chaque schema de ``existing_schemas``, l'ensemble
            des tables qu'il contient reellement (``information_schema.tables``).
            ``None`` ou absence d'une entree pour un schema donne desactive le
            check de completude pour ce schema (aucun faux-positif si
            l'appelant n'a pas fourni cette donnee).
    """
    schema_tables = schema_tables or {}

    long_slug_tenants = [
        t for t in tenants if len(t.slug) > TENANT_SLUG_MAX_LENGTH_FOR_CREATION
    ]

    groups_by_physical_schema: dict[str, list[TenantRow]] = {}
    for t in tenants:
        groups_by_physical_schema.setdefault(t.expected_physical_schema, []).append(t)

    truncation_collision_groups = [
        group for group in groups_by_physical_schema.values() if len(group) > 1
    ]

    tenants_missing_schema = [
        t for t in tenants if t.expected_physical_schema not in existing_schemas
    ]

    matched_schemas = set(groups_by_physical_schema.keys())
    orphan_schemas = sorted(existing_schemas - matched_schemas)

    incomplete_schemas = []
    for schema in sorted(existing_schemas):
        if schema not in schema_tables:
            continue
        missing = EXPECTED_TENANT_TABLES - schema_tables[schema]
        if missing:
            incomplete_schemas.append(IncompleteSchema(schema=schema, missing_tables=sorted(missing)))

    return AuditReport(
        total_tenants=len(tenants),
        total_tenant_schemas=len(existing_schemas),
        long_slug_tenants=long_slug_tenants,
        truncation_collision_groups=truncation_collision_groups,
        tenants_missing_schema=tenants_missing_schema,
        orphan_schemas=orphan_schemas,
        incomplete_schemas=incomplete_schemas,
    )


async def _fetch_tenants(conn) -> list[TenantRow]:
    result = await conn.execute(text("SELECT id, slug, name FROM public.tenants ORDER BY id"))
    return [TenantRow(id=row.id, slug=row.slug, name=row.name) for row in result]


async def _fetch_tenant_schemas(conn) -> set[str]:
    result = await conn.execute(
        text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant\\_%' ESCAPE '\\'")
    )
    return {row.nspname for row in result}


async def _fetch_tenant_schema_tables(conn) -> dict[str, set[str]]:
    """Pour chaque schema ``tenant_*``, l'ensemble des tables qu'il contient
    reellement -- une seule requete groupee plutot qu'une par schema."""
    result = await conn.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema LIKE 'tenant\\_%' ESCAPE '\\'"
        )
    )
    tables_by_schema: dict[str, set[str]] = {}
    for row in result:
        tables_by_schema.setdefault(row.table_schema, set()).add(row.table_name)
    return tables_by_schema


async def run_audit(database_url: str) -> AuditReport:
    """Ouvre une connexion en lecture seule et produit le rapport d'audit.

    [SECURITE] ``postgresql_readonly=True`` fait emettre
    ``SET TRANSACTION READ ONLY`` par le driver : PostgreSQL rejette lui-meme
    toute tentative d'ecriture sur cette connexion (garantie appliquee cote
    base, pas seulement "le code ne fait que des SELECT").
    """
    engine = create_async_engine(database_url, echo=False)
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(postgresql_readonly=True)
            tenants = await _fetch_tenants(conn)
            schemas = await _fetch_tenant_schemas(conn)
            schema_tables = await _fetch_tenant_schema_tables(conn)
    finally:
        await engine.dispose()

    return build_report(tenants, schemas, schema_tables)


def _tenant_label(t: TenantRow) -> str:
    return f"id={t.id} slug={t.slug!r} name={t.name!r}"


def format_report_text(report: AuditReport) -> str:
    lines: list[str] = []
    lines.append("=== Audit tenants PostgreSQL (lecture seule) ===")
    lines.append(f"Tenants dans public.tenants : {report.total_tenants}")
    lines.append(f"Schemas tenant_* dans pg_namespace : {report.total_tenant_schemas}")
    lines.append("")

    lines.append(f"1. Slugs > {TENANT_SLUG_MAX_LENGTH_FOR_CREATION} caracteres (limite de creation)")
    if report.long_slug_tenants:
        for t in report.long_slug_tenants:
            lines.append(f"   [ANOMALIE] {_tenant_label(t)} -- longueur={len(t.slug)}")
    else:
        lines.append("   OK -- aucun.")
    lines.append("")

    lines.append("2. Couples de tenants partageant le meme prefixe de schema a 63 octets")
    if report.truncation_collision_groups:
        for group in report.truncation_collision_groups:
            schema = group[0].expected_physical_schema
            lines.append(f"   [COLLISION] schema physique '{schema}' partage par :")
            for t in group:
                lines.append(f"       - {_tenant_label(t)}")
    else:
        lines.append("   OK -- aucune collision detectee.")
    lines.append("")

    lines.append("3a. Tenants enregistres sans schema physique correspondant")
    if report.tenants_missing_schema:
        for t in report.tenants_missing_schema:
            lines.append(
                f"   [ANOMALIE] {_tenant_label(t)} -- schema attendu "
                f"'{t.expected_physical_schema}' absent de pg_namespace"
            )
    else:
        lines.append("   OK -- tous les tenants ont un schema physique.")
    lines.append("")

    lines.append("3b. Schemas tenant_* sans ligne public.tenants correspondante (orphelins)")
    if report.orphan_schemas:
        for schema in report.orphan_schemas:
            lines.append(f"   [ANOMALIE] schema '{schema}' sans tenant correspondant")
    else:
        lines.append("   OK -- aucun schema orphelin.")
    lines.append("")

    lines.append("4. Schemas tenant_* incomplets (tables attendues manquantes)")
    if report.incomplete_schemas:
        for entry in report.incomplete_schemas:
            lines.append(f"   [ANOMALIE] schema '{entry.schema}' -- tables manquantes : {', '.join(entry.missing_tables)}")
    else:
        lines.append("   OK -- tous les schemas verifies contiennent toutes les tables attendues.")
    lines.append("")

    lines.append(
        "RESULTAT : " + ("ANOMALIES DETECTEES -- ne pas deployer sans investiguer." if report.has_issues else "OK, aucune anomalie.")
    )
    return "\n".join(lines)


def format_report_json(report: AuditReport) -> str:
    def tenant_dict(t: TenantRow) -> dict:
        return {"id": t.id, "slug": t.slug, "name": t.name, "expected_physical_schema": t.expected_physical_schema}

    payload = {
        "total_tenants": report.total_tenants,
        "total_tenant_schemas": report.total_tenant_schemas,
        "has_issues": report.has_issues,
        "long_slug_tenants": [tenant_dict(t) for t in report.long_slug_tenants],
        "truncation_collision_groups": [
            [tenant_dict(t) for t in group] for group in report.truncation_collision_groups
        ],
        "tenants_missing_schema": [tenant_dict(t) for t in report.tenants_missing_schema],
        "orphan_schemas": report.orphan_schemas,
        "incomplete_schemas": [
            {"schema": e.schema, "missing_tables": e.missing_tables} for e in report.incomplete_schemas
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _main_async(args: argparse.Namespace) -> int:
    database_url = args.database_url or settings.database_url
    try:
        report = await run_audit(database_url)
    except Exception as exc:  # connexion/execution -- jamais une anomalie de donnees
        print(f"[ERREUR] Audit impossible : {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))

    return 1 if report.has_issues else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit en lecture seule des tenants PostgreSQL (collisions de schema, coherence public.tenants/pg_namespace)."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="URL PostgreSQL a auditer (defaut : DATABASE_URL / settings.database_url).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Format de sortie (defaut : text).",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(_main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
