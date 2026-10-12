"""Real PostgreSQL invariants for the additive phase-1 schema and frozen migration."""

import importlib.util
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.database import Base
from app.core.tenancy.provisioning import _provision_tenant_schema
from app.modules.haccp import foundation_models as h
from app.modules.haccp.foundation_contracts import (
    ChecklistConfirmation,
    ClosureRegularizationRequest,
)
from app.modules.haccp.foundation_diagnostics import inspect_foundations
from app.modules.haccp.models import (
    HaccpEquipment,
    HaccpCheckSession,
    HaccpCleaningTask,
)
from app.modules.admin.tenants.models import (
    BusinessHours,
    ExceptionalClosure,
    TenantConfig,
)
from app.modules.hr.models import Establishment
from app.modules.orders.models import Order
from app.modules.stock import site_models as s
from app.modules.stock.models import Ingredient, IngredientBatch

NEW_TABLES = {
    value.__table__.name
    for module in (h, s)
    for value in vars(module).values()
    if isinstance(value, type) and hasattr(value, "__table__")
}


async def add(session, model, **values):
    statement = sa.insert(model).values(**values)
    if "id" in model.__table__.c:
        return await session.scalar(statement.returning(model.id))
    await session.execute(statement)


@pytest.fixture
async def graph(db_session):
    site_a = await add(db_session, Establishment, name="A")
    site_b = await add(db_session, Establishment, name="B")
    ingredient = await add(
        db_session, Ingredient, name="Mozzarella", unit="kg", current_qty=10
    )
    for site in (site_a, site_b):
        await add(
            db_session,
            s.EstablishmentStock,
            establishment_id=site,
            ingredient_id=ingredient,
            physical_quantity=5,
        )
    batch = await add(
        db_session,
        IngredientBatch,
        ingredient_id=ingredient,
        quantity=5,
        received_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    await add(
        db_session,
        s.StockBatchPosition,
        batch_id=batch,
        establishment_id=site_a,
        ingredient_id=ingredient,
    )
    session_id = await add(
        db_session,
        h.HaccpDailySession,
        establishment_id=site_a,
        business_date=date(2026, 10, 3),
        session_type="opening",
        timezone="Europe/Paris",
    )
    requirement = await add(
        db_session,
        h.HaccpRequirement,
        establishment_id=site_a,
        session_id=session_id,
        session_revision=1,
        requirement_key="ingredient:1",
        kind="dlc",
        ingredient_id=ingredient,
        snapshot={"quantity": "5"},
    )
    return {
        "a": site_a,
        "b": site_b,
        "ingredient": ingredient,
        "batch": batch,
        "session": session_id,
        "requirement": requirement,
    }


async def rejected(session, model, *, sqlstate=None, **values):
    with pytest.raises(DBAPIError if sqlstate else IntegrityError) as error:
        async with session.begin_nested():
            await add(session, model, **values)
    if sqlstate:
        assert error.value.orig.sqlstate == sqlstate


async def test_daily_uniqueness_is_per_establishment(db_session, graph):
    values = dict(
        business_date=date(2026, 10, 3), session_type="opening", timezone="Europe/Paris"
    )
    await add(db_session, h.HaccpDailySession, establishment_id=graph["b"], **values)
    await rejected(
        db_session, h.HaccpDailySession, establishment_id=graph["a"], **values
    )
    await add(
        db_session,
        h.HaccpDailySession,
        establishment_id=graph["a"],
        **{**values, "session_type": "closing"},
    )


async def test_equipment_and_evidence_cannot_cross_sites(db_session, graph):
    equipment = await add(db_session, HaccpEquipment, name="Frigo B", type="fridge")
    await add(
        db_session,
        h.HaccpEquipmentScope,
        equipment_id=equipment,
        establishment_id=graph["b"],
    )
    await rejected(
        db_session,
        h.HaccpRequirement,
        session_id=graph["session"],
        establishment_id=graph["a"],
        session_revision=1,
        requirement_key="fridge",
        kind="temperature",
        equipment_id=equipment,
        snapshot={},
    )
    await rejected(
        db_session,
        h.HaccpEvidence,
        requirement_id=graph["requirement"],
        session_id=graph["session"],
        establishment_id=graph["b"],
        idempotency_key="request1",
        is_compliant=True,
        payload={},
        recorded_by=7,
    )


async def test_dlc_lot_must_match_ingredient_and_site(db_session, graph):
    await add(
        db_session,
        h.HaccpRequirementBatch,
        requirement_id=graph["requirement"],
        batch_id=graph["batch"],
        establishment_id=graph["a"],
        ingredient_id=graph["ingredient"],
        batch_revision=1,
    )
    wrong = await add(db_session, Ingredient, name="Other", unit="kg", current_qty=1)
    # The same lot is intentionally allowed at another establishment; the
    # position key is scoped by establishment and ingredient.
    await add(
        db_session,
        s.StockBatchPosition,
        batch_id=graph["batch"],
        establishment_id=graph["b"],
        ingredient_id=graph["ingredient"],
        remaining_quantity=5,
    )
    await rejected(
        db_session,
        s.StockBatchPosition,
        batch_id=graph["batch"],
        establishment_id=graph["b"],
        ingredient_id=wrong,
        remaining_quantity=5,
    )


async def test_unknown_remainder_is_preserved_and_exclusion_needs_proof(
    db_session, graph
):
    position = await db_session.scalar(
        sa.select(s.StockBatchPosition).where(
            s.StockBatchPosition.batch_id == graph["batch"],
            s.StockBatchPosition.establishment_id == graph["a"],
            s.StockBatchPosition.ingredient_id == graph["ingredient"],
        )
    )
    assert position.remaining_quantity is None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.update(s.StockBatchPosition)
                .where(s.StockBatchPosition.batch_id == graph["batch"])
                .values(excluded_at=datetime.now(timezone.utc), excluded_by=7)
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.update(s.StockBatchPosition)
                .where(s.StockBatchPosition.batch_id == graph["batch"])
                .values(remaining_quantity=-1)
            )


async def test_allocations_enforce_order_site_quantity_and_idempotency(
    db_session, graph
):
    order_a = await add(
        db_session, Order, establishment_id=graph["a"], order_type="pickup"
    )
    order_b = await add(
        db_session, Order, establishment_id=graph["b"], order_type="pickup"
    )
    values = dict(
        batch_id=graph["batch"],
        establishment_id=graph["a"],
        ingredient_id=graph["ingredient"],
        quantity=Decimal("0.125"),
    )
    await rejected(db_session, s.StockBatchAllocation, order_id=order_b, **values)
    allocation = await add(
        db_session, s.StockBatchAllocation, order_id=order_a, **values
    )
    await rejected(db_session, s.StockBatchAllocation, order_id=order_a, **values)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.update(s.StockBatchAllocation)
                .where(s.StockBatchAllocation.id == allocation)
                .values(restored_quantity=Decimal("0.126"))
            )


@pytest.mark.parametrize("status", ["complete", "regularized", "incomplete_validated"])
async def test_session_cannot_claim_unattributed_or_forced_validation(
    db_session, graph, status
):
    await rejected(
        db_session,
        h.HaccpDailySession,
        sqlstate="22001" if status == "incomplete_validated" else "23514",
        establishment_id=graph["b"],
        business_date=date(2026, 10, 3),
        session_type="opening",
        timezone="Europe/Paris",
        status=status,
    )


async def test_regularization_only_targets_a_closing_and_keeps_real_time(
    db_session, graph
):
    values = dict(
        establishment_id=graph["a"],
        reason="Oubli",
        actions_taken="Controle effectue maintenant",
        recorded_by=7,
    )
    await rejected(
        db_session, h.HaccpRegularization, closing_session_id=graph["session"], **values
    )
    closing = await add(
        db_session,
        h.HaccpDailySession,
        establishment_id=graph["a"],
        business_date=date(2020, 1, 1),
        session_type="closing",
        timezone="Europe/Paris",
    )
    row = await add(
        db_session, h.HaccpRegularization, closing_session_id=closing, **values
    )
    proof = await db_session.get(h.HaccpRegularization, row)
    assert proof.recorded_at.date() > date(2020, 1, 1)
    await rejected(
        db_session, h.HaccpRegularization, closing_session_id=closing, **values
    )


async def test_reminders_deduplicate_per_site_schedule_recipient_channel(
    db_session, graph
):
    values = dict(
        establishment_id=graph["a"],
        business_date=date(2026, 10, 3),
        schedule_revision=1,
        due_at=datetime(2026, 10, 3, 9, 10, tzinfo=timezone.utc),
        user_id=7,
        channel="push",
    )
    await add(db_session, h.HaccpReminderDelivery, **values)
    await rejected(db_session, h.HaccpReminderDelivery, **values)
    await add(db_session, h.HaccpReminderDelivery, **{**values, "channel": "in_app"})
    await rejected(
        db_session,
        h.HaccpReminderDelivery,
        **{**values, "user_id": 8, "status": "sent"},
    )


async def test_no_fabricated_legacy_approval_and_read_only_diagnostic(
    db_session, graph
):
    await add(
        db_session,
        HaccpCheckSession,
        date=date(2026, 10, 2),
        session_type="opening",
        status="complete",
    )
    report = await inspect_foundations(db_session)
    assert not report.activation_available and not report.strict_enabled
    assert report.counts["legacy_sessions"] == 1
    assert (
        report.counts["strict_sessions"] == 1
    )  # only the explicit fixture, not legacy conversion
    assert report.counts["unknown_batch_remainders"] == 1
    assert "explicit_establishment_assignment_required" in report.blockers
    assert not db_session.new and not db_session.dirty


@pytest.mark.parametrize(
    "injected", ["force", "recorded_at", "recorded_by", "is_compliant"]
)
def test_confirmation_contract_rejects_client_asserted_authority(injected):
    payload = dict(
        establishment_id=1,
        session_id=1,
        expected_revision=1,
        requirement_ids=[1],
        idempotency_key="abc",
    )
    with pytest.raises(ValidationError):
        ChecklistConfirmation(**payload, **{injected: True})


@pytest.mark.parametrize("requirements", [[], [0], [-1], [1, 1]])
def test_confirmation_contract_rejects_invalid_requirement_sets(requirements):
    with pytest.raises(ValidationError):
        ChecklistConfirmation(
            establishment_id=1,
            session_id=1,
            expected_revision=1,
            requirement_ids=requirements,
            idempotency_key="abc",
        )


def test_regularization_request_cannot_backdate():
    with pytest.raises(ValidationError):
        ClosureRegularizationRequest(
            establishment_id=1,
            closing_session_id=1,
            expected_revision=1,
            reason="Oubli",
            actions_taken="Controle",
            idempotency_key="abc",
            recorded_at="2020-01-01",
        )


def load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/haccp_p1_20261003_foundations.py"
    )
    spec = importlib.util.spec_from_file_location("haccp_foundation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_metadata():
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in NEW_TABLES:
            copy = table.to_metadata(metadata)
            for constraint in list(copy.constraints):
                if constraint.name in {
                    "uq_orders_id_establishment",
                    "uq_batch_id_ingredient",
                }:
                    copy.constraints.remove(constraint)
    return metadata


def fingerprint(connection, schema):
    inspector = sa.inspect(connection)
    result = {}
    for table in sorted(NEW_TABLES | {"orders", "ingredient_batches"}):
        # A migrated tenant must never reference another tenant or public tables.
        if table in NEW_TABLES:
            assert all(
                fk["referred_schema"] in (None, schema)
                for fk in inspector.get_foreign_keys(table, schema=schema)
            )
        result[table] = {
            "columns": [
                (
                    c["name"],
                    str(c["type"]),
                    c["nullable"],
                    "sequence"
                    if c["default"] and "nextval" in c["default"]
                    else c["default"],
                )
                for c in inspector.get_columns(table, schema=schema)
            ],
            "unique": sorted(
                (c["name"] or "", tuple(c["column_names"]))
                for c in inspector.get_unique_constraints(table, schema=schema)
            ),
            "checks": sorted(
                (c["name"] or "", c["sqltext"])
                for c in inspector.get_check_constraints(table, schema=schema)
            ),
            "fk": sorted(
                (
                    tuple(c["constrained_columns"]),
                    c["referred_table"],
                    tuple(c["referred_columns"]),
                )
                for c in inspector.get_foreign_keys(table, schema=schema)
            ),
            "indexes": sorted(
                (c["name"], tuple(c["column_names"]), c["unique"])
                for c in inspector.get_indexes(table, schema=schema)
            ),
        }
    return result


@pytest.mark.parametrize("site_count", [0, 1, 2])
async def test_migration_preserves_legacy_and_matches_new_tenant(db_engine, site_count):
    migration = load_migration()
    suffix = uuid.uuid4().hex[:12]
    old_schema, fresh_slug = f"tenant_hcold{suffix}", f"hcnew{suffix}"
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text(f'CREATE SCHEMA "{old_schema}"'))
            await connection.execute(
                sa.text(f'SET LOCAL search_path TO "{old_schema}"')
            )
            await connection.run_sync(lambda conn: legacy_metadata().create_all(conn))
            for number in range(site_count):
                await connection.execute(
                    sa.insert(Establishment).values(name=f"Site {number}")
                )
            await connection.execute(
                sa.insert(HaccpEquipment).values(name="Frigo", type="fridge")
            )
            await connection.execute(
                sa.insert(HaccpCleaningTask).values(
                    name="Sol", zone="Cuisine", frequency="weekly"
                )
            )
            await connection.execute(
                sa.insert(TenantConfig).values(
                    is_temporarily_closed=True, default_closure_message="Fermeture test"
                )
            )
            await connection.execute(
                sa.insert(ExceptionalClosure).values(
                    closure_date=date(2026, 12, 25), use_default_message=True
                )
            )
            await connection.execute(
                sa.insert(BusinessHours),
                [
                    dict(
                        day_of_week=0,
                        slot_index=0,
                        opens_at=time(11),
                        closes_at=time(14),
                    ),
                    dict(
                        day_of_week=1,
                        slot_index=0,
                        opens_at=time(11),
                        closes_at=time(14),
                    ),
                    dict(
                        day_of_week=1,
                        slot_index=0,
                        opens_at=time(12),
                        closes_at=time(15),
                    ),
                ],
            )
            ingredient = await connection.scalar(
                sa.insert(Ingredient)
                .values(name="Legacy", unit="kg", current_qty=Decimal("7.250"))
                .returning(Ingredient.id)
            )
            await connection.execute(
                sa.insert(IngredientBatch).values(
                    ingredient_id=ingredient,
                    quantity=10,
                    received_at=datetime.now(timezone.utc),
                    status="sealed",
                )
            )
            await connection.execute(
                sa.insert(HaccpCheckSession).values(
                    date=date(2026, 10, 2), session_type="opening", status="complete"
                )
            )
            previous_path = await connection.scalar(sa.text("SHOW search_path"))

            def migrate(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    migration._upgrade_schema(conn, old_schema)

            await connection.run_sync(migrate)
            assert await connection.scalar(sa.text("SHOW search_path")) == previous_path
            assert await connection.scalar(
                sa.text("SELECT current_qty FROM ingredients")
            ) == Decimal("7.250")
            assert (
                await connection.scalar(
                    sa.text("SELECT quantity FROM ingredient_batches")
                )
                == 10
            )
            assert (
                await connection.scalar(
                    sa.text("SELECT count(*) FROM haccp_daily_sessions")
                )
                == 0
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM establishment_operations WHERE strict_enabled"
                    )
                )
                == 0
            )
            assert await connection.scalar(
                sa.text("SELECT count(*) FROM establishment_stock")
            ) == (1 if site_count == 1 else 0)
            for table in (
                "haccp_equipment_scopes",
                "haccp_cleaning_scopes",
                "establishment_business_hours",
                "establishment_closures",
            ):
                assert await connection.scalar(
                    sa.text(f"SELECT count(*) FROM {table}")
                ) == (1 if site_count == 1 else 0)
            if site_count == 1:
                assert (
                    await connection.scalar(
                        sa.text("SELECT message FROM establishment_closures")
                    )
                    == "Fermeture test"
                )
                assert (
                    await connection.scalar(
                        sa.text(
                            "SELECT is_temporarily_closed FROM establishment_operations"
                        )
                    )
                    is True
                )
                assert (
                    await connection.scalar(
                        sa.text("SELECT due_from FROM haccp_cleaning_scopes")
                    )
                    is None
                )
                assert await connection.scalar(
                    sa.text("SELECT physical_quantity FROM establishment_stock")
                ) == Decimal("7.250")
                assert (
                    await connection.scalar(
                        sa.text("SELECT remaining_quantity FROM stock_batch_positions")
                    )
                    is None
                )
                assert (
                    await connection.scalar(
                        sa.text("SELECT reconciled_at FROM establishment_stock")
                    )
                    is None
                )
            # Running against a newly provisioned schema or repeating before version stamp is safe.
            await connection.run_sync(migrate)
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM haccp_check_sessions WHERE status='complete'"
                    )
                )
                == 1
            )
            old_fingerprint = await connection.run_sync(
                lambda conn: fingerprint(conn, old_schema)
            )
            await connection.execute(sa.text(f'CREATE SCHEMA "tenant_{fresh_slug}"'))
            await _provision_tenant_schema(connection, fresh_slug)
            fresh_fingerprint = await connection.run_sync(
                lambda conn: fingerprint(conn, f"tenant_{fresh_slug}")
            )
            assert old_fingerprint == fresh_fingerprint
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM establishment_operations WHERE strict_enabled"
                    )
                )
                == 0
            )
        finally:
            await transaction.rollback()


def test_downgrade_refuses_to_destroy_evidence():
    with pytest.raises(RuntimeError, match="forward-only"):
        load_migration().downgrade()
