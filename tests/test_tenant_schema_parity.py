"""Verifie que le schema d'un tenant fraichement provisionne (via
``_provision_tenant_schema``, cf. ``app/modules/auth/service.py``) est
strictement identique — tables, colonnes, contraintes, index — a celui
d'un tenant existant entierement migre via Alembic.

Empeche la reapparition de la divergence P5 (audit-consolidated-plan.md) :
avant ce test, le schema tenant etait duplique entre les migrations Alembic
et une liste ``_TENANT_DDL_STATEMENTS`` maintenue a la main, qui avait
divergé silencieusement de plusieurs migrations (tables loyalty_config /
loyalty_rewards / loyalty_rules / loyalty_point_reservations, module
restaurant_delivery_settings, colonne products.is_delivery_prohibited —
absentes du DDL de provisioning alors qu'elles existaient déjà en
production pour les tenants migrés). Le provisioning utilise maintenant
``Base.metadata`` (``app/core/database/tenant_models.py``), la meme source
que Alembic — ce test verifie que les deux chemins produisent un schema
identique.

Necessite une connexion PostgreSQL reelle avec au moins un schema
``tenant_*`` deja migre a head (utilise comme reference).
"""

import pytest
from sqlalchemy import text

from app.core.database import tenant_schema_name
from app.modules.auth.service import _provision_tenant_schema

NEW_TENANT_SLUG = "ddlparitytest"
BOOTSTRAP_LEGACY_SCHEMA = "tenant_pizza_test"
BOOTSTRAP_TEST_SCHEMA = "tenant_test"
BOOTSTRAP_INTEGRATION_SCHEMA = "tenant_default"

# Tables intentionnellement absentes du provisioning neuf : legacy en cours de
# depreciation via une initiative independante de ce fix (voir
# tests/test_db_phase1.py::test_product_stock_removed, DB-05 — ProductStock ne
# doit plus etre defini dans app/modules/stock/models.py). Le schema des
# tenants deja migres l'a encore tant que la migration de suppression n'a pas
# ete ecrite ; ce n'est pas une divergence a corriger ici.
LEGACY_TABLES_NOT_PROVISIONED_FOR_NEW_TENANTS = {"product_stock"}


def _norm_default(value: str | None) -> str | None:
    """Neutralise les noms de sequence qualifies par schema (artefact de
    comparaison entre deux schemas differents, pas une vraie divergence)."""
    if value is not None and "_id_seq" in value:
        return "nextval(<seq>)"
    return value


async def _introspect(conn, schema: str) -> dict:
    tables = (
        await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema=:s"),
            {"s": schema},
        )
    ).scalars().all()

    cols = (
        await conn.execute(
            text(
                """SELECT table_name, column_name, data_type, is_nullable, column_default
                   FROM information_schema.columns WHERE table_schema=:s"""
            ),
            {"s": schema},
        )
    ).fetchall()

    indexes = (
        await conn.execute(
            text("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname=:s"),
            {"s": schema},
        )
    ).fetchall()

    constraints = (
        await conn.execute(
            text(
                """SELECT rel.relname, con.contype, pg_get_constraintdef(con.oid)
                   FROM pg_constraint con
                   JOIN pg_class rel ON rel.oid = con.conrelid
                   JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                   WHERE nsp.nspname = :s"""
            ),
            {"s": schema},
        )
    ).fetchall()

    return {
        "tables": set(tables),
        "cols": {(r[0], r[1]): (r[2], r[3], _norm_default(r[4])) for r in cols},
        # normalise "USING btree (x)" -> "btree (x)" ; le nom de l'index n'est pas
        # significatif (les migrations historiques suffixent par slug, le
        # provisioning neuf non — seule la forme structurelle compte ici).
        "indexes": {(r[0], r[1].split(" USING ", 1)[-1]) for r in indexes},
        # le nom de contrainte n'est pas compare, seule sa definition structurelle
        # (colonnes, ON DELETE, expression CHECK...) ; le prefixe de schema dans
        # les FK est normalise pour comparer deux schemas de noms differents.
        "constraints": {(r[0], r[1], r[2].replace(f"{schema}.", "")) for r in constraints},
    }


@pytest.mark.asyncio
async def test_new_tenant_schema_matches_fully_migrated_tenant(db_engine):
    async with db_engine.connect() as conn:
        reference_schema = (
            await conn.execute(
                text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant\\_%' ESCAPE '\\' "
                    "AND nspname NOT IN (:bootstrap_schema, :default_schema, :integration_schema) "
                    "ORDER BY nspname LIMIT 1"
                ),
                {
                    "bootstrap_schema": BOOTSTRAP_LEGACY_SCHEMA,
                    "default_schema": BOOTSTRAP_TEST_SCHEMA,
                    "integration_schema": BOOTSTRAP_INTEGRATION_SCHEMA,
                },
            )
        ).scalar()
    assert reference_schema, (
        "Aucun schema tenant migre trouve dans la base de test — "
        "ce test compare le provisioning neuf a un tenant deja migre par Alembic."
    )

    new_schema = tenant_schema_name(NEW_TENANT_SLUG)
    async with db_engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{new_schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{new_schema}"'))
        await _provision_tenant_schema(conn, NEW_TENANT_SLUG)

    try:
        async with db_engine.connect() as conn:
            new_tenant = await _introspect(conn, new_schema)
            migrated_tenant = await _introspect(conn, reference_schema)

        migrated_tenant["tables"] -= LEGACY_TABLES_NOT_PROVISIONED_FOR_NEW_TENANTS
        migrated_tenant["cols"] = {
            k: v for k, v in migrated_tenant["cols"].items()
            if k[0] not in LEGACY_TABLES_NOT_PROVISIONED_FOR_NEW_TENANTS
        }
        migrated_tenant["constraints"] = {
            c for c in migrated_tenant["constraints"] if c[0] not in LEGACY_TABLES_NOT_PROVISIONED_FOR_NEW_TENANTS
        }
        migrated_tenant["indexes"] = {
            i for i in migrated_tenant["indexes"] if i[0] not in LEGACY_TABLES_NOT_PROVISIONED_FOR_NEW_TENANTS
        }

        assert new_tenant["tables"] == migrated_tenant["tables"], (
            f"tables manquantes cote provisioning neuf: {migrated_tenant['tables'] - new_tenant['tables']} | "
            f"tables en trop: {new_tenant['tables'] - migrated_tenant['tables']}"
        )

        missing_cols = set(migrated_tenant["cols"]) - set(new_tenant["cols"])
        extra_cols = set(new_tenant["cols"]) - set(migrated_tenant["cols"])
        differing_cols = {
            k: (new_tenant["cols"][k], migrated_tenant["cols"][k])
            for k in set(new_tenant["cols"]) & set(migrated_tenant["cols"])
            if new_tenant["cols"][k] != migrated_tenant["cols"][k]
        }
        assert not missing_cols and not extra_cols and not differing_cols, (
            f"colonnes manquantes: {missing_cols} | colonnes en trop: {extra_cols} | "
            f"colonnes divergentes (type/nullable/default) — neuf vs migre: {differing_cols}"
        )

        assert new_tenant["constraints"] == migrated_tenant["constraints"], (
            f"contraintes manquantes: {migrated_tenant['constraints'] - new_tenant['constraints']} | "
            f"contraintes en trop: {new_tenant['constraints'] - migrated_tenant['constraints']}"
        )

        assert new_tenant["indexes"] == migrated_tenant["indexes"], (
            f"index manquants: {migrated_tenant['indexes'] - new_tenant['indexes']} | "
            f"index en trop: {new_tenant['indexes'] - migrated_tenant['indexes']}"
        )
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{new_schema}" CASCADE'))
