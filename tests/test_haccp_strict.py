"""Strict daily workflow, real PostgreSQL and a controlled server clock."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.core.http.deps import get_current_user
from app.core.http.errors import AppError
from app.core.database import get_tenant_session
from app.core.tenancy.provisioning import _provision_tenant_schema
from app.main import app
from app.modules.haccp import foundation_models as h
from app.modules.haccp import strict_router, strict_service as service
from app.modules.haccp.models import HaccpCleaningTask, HaccpEquipment
from app.modules.haccp.strict_checklist import cycle_start
from app.modules.haccp.strict_contracts import (
    ExplainRequest,
    RecordEvidence,
    RevisionRequest,
)
from app.modules.hr.models import Establishment
from app.modules.stock.models import Ingredient, IngredientBatch
from app.modules.stock.site_models import EstablishmentStock, StockBatchPosition

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
ADMIN = {"id": 7, "role": "admin", "tenant_slug": "test"}


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=NOW)
    monkeypatch.setattr(service, "utcnow", lambda: state.now)
    return state


@pytest.fixture
async def ready(db_session, clock):
    return await seed_ready(db_session)


async def seed_ready(db_session):
    site = Establishment(name="Strict A", timezone="UTC")
    equipment = HaccpEquipment(
        name="Frigo", type="fridge", target_min_temp=0, target_max_temp=4
    )
    task = HaccpCleaningTask(name="Sol", zone="Cuisine", frequency="daily")
    ingredient = Ingredient(name="Mozza", unit="kg", current_qty=2)
    db_session.add_all([site, equipment, task, ingredient])
    await db_session.flush()
    stock = EstablishmentStock(
        establishment_id=site.id,
        ingredient_id=ingredient.id,
        physical_quantity=2,
        reconciled_at=NOW,
        reconciled_by=7,
    )
    batch = IngredientBatch(
        ingredient_id=ingredient.id,
        quantity=2,
        received_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    operations = h.EstablishmentOperations(
        establishment_id=site.id, activated_at=NOW - timedelta(days=3)
    )
    db_session.add_all(
        [
            stock,
            batch,
            operations,
            h.HaccpEquipmentScope(equipment_id=equipment.id, establishment_id=site.id),
            h.HaccpCleaningScope(task_id=task.id, establishment_id=site.id),
        ]
    )
    for weekday in range(7):
        db_session.add(
            h.EstablishmentBusinessHours(
                establishment_id=site.id,
                day_of_week=weekday,
                slot_index=0,
                opens_at=time(11),
                closes_at=time(23),
            )
        )
    await db_session.flush()
    position = StockBatchPosition(
        batch_id=batch.id,
        establishment_id=site.id,
        ingredient_id=ingredient.id,
        remaining_quantity=2,
    )
    db_session.add(position)
    await db_session.flush()
    return SimpleNamespace(
        site=site,
        equipment=equipment,
        task=task,
        ingredient=ingredient,
        stock=stock,
        batch=batch,
        position=position,
        operations=operations,
    )


def evidence(report, key="evidence", temperature=3):
    return RecordEvidence(
        expected_revision=report["revision"],
        idempotency_key=key,
        items=[
            {
                "requirement_id": item["id"],
                **(
                    {"temperature": temperature}
                    if item["kind"] == "temperature"
                    else {"confirmed": True}
                ),
            }
            for item in report["requirements"]
        ],
    )


async def prove(session, ready, kind="opening", *, finish=True):
    report = await service.start(session, ready.site, kind, ADMIN)
    row = await service.get_check(session, ready.site.id, report["id"])
    report = await service.record(session, ready.site, row, evidence(report), ADMIN)
    if finish:
        report = await service.complete(
            session,
            ready.site,
            row,
            RevisionRequest(expected_revision=row.revision, idempotency_key="finish"),
            ADMIN,
        )
    return row, report


def codes(report):
    return {item["code"] for item in report["blockers"]}


async def test_all_requirements_required_and_open_close_decision(db_session, ready):
    initial = await service.start(db_session, ready.site, "opening", ADMIN)
    assert len(initial["requirements"]) == 3
    assert {
        "HACCP_TEMPERATURE_MISSING",
        "HACCP_CLEANING_MISSING",
        "HACCP_DLC_CONFIRMATION_MISSING",
    } <= codes(initial)
    row = await service.get_check(db_session, ready.site.id, initial["id"])
    result = await service.complete(
        db_session,
        ready.site,
        row,
        RevisionRequest(expected_revision=1, idempotency_key="incomplete"),
        ADMIN,
    )
    assert result["status"] == "in_progress"
    row, result = await prove(db_session, ready)
    assert row.status == "complete" and not result["blockers"]
    decision = await service.decision(db_session, ready.site)
    assert decision["daily_haccp_satisfied"] and not decision["ordering_enforced"]
    await prove(db_session, ready, "closing")
    assert "HACCP_DAY_CLOSED" in codes(await service.decision(db_session, ready.site))


async def test_noncompliant_temperature_needs_traced_correction(db_session, ready):
    report = await service.start(db_session, ready.site, "opening", ADMIN)
    row = await service.get_check(db_session, ready.site.id, report["id"])
    bad = await service.record(
        db_session, ready.site, row, evidence(report, temperature=9), ADMIN
    )
    assert "HACCP_TEMPERATURE_NONCOMPLIANT" in codes(bad)
    temp = next(
        item for item in report["requirements"] if item["kind"] == "temperature"
    )
    body = RecordEvidence(
        expected_revision=1,
        idempotency_key="correct",
        items=[{"requirement_id": temp["id"], "temperature": 3}],
    )
    with pytest.raises(AppError) as error:
        await service.record(db_session, ready.site, row, body, ADMIN)
    assert error.value.code == "HACCP_CORRECTION_REQUIRED"
    body.items[0].corrective_action = "Réglage effectué et nouveau relevé"
    good = await service.record(db_session, ready.site, row, body, ADMIN)
    assert good["can_validate"]
    assert (
        await db_session.scalar(select(func.count()).select_from(h.HaccpEvidence)) == 4
    )


async def test_repeat_is_idempotent_but_changed_payload_rejected(db_session, ready):
    row, report = await prove(db_session, ready, finish=False)
    body = evidence(report)
    await service.record(db_session, ready.site, row, body, ADMIN)
    assert (
        await db_session.scalar(select(func.count()).select_from(h.HaccpEvidence)) == 3
    )
    body.items[0].confirmed = False
    with pytest.raises(AppError) as error:
        await service.record(db_session, ready.site, row, body, ADMIN)
    assert error.value.code == "HACCP_IDEMPOTENCY_CONFLICT"


async def test_changed_lot_requires_new_revision_and_human_confirmation(
    db_session, ready
):
    row, old = await prove(db_session, ready, finish=False)
    ready.batch.expires_at += timedelta(
        days=1
    )  # Legacy writer does not know position.revision.
    await db_session.flush()
    with pytest.raises(AppError) as error:
        await service.complete(
            db_session,
            ready.site,
            row,
            RevisionRequest(expected_revision=1, idempotency_key="finish"),
            ADMIN,
        )
    assert error.value.code == "HACCP_EVIDENCE_STALE"
    refreshed = await service.start(db_session, ready.site, "opening", ADMIN)
    assert refreshed["revision"] == 2 and not refreshed["can_validate"]
    with pytest.raises(AppError) as error:
        await service.record(db_session, ready.site, row, evidence(old), ADMIN)
    assert error.value.code == "HACCP_EVIDENCE_STALE"
    assert all(item["evidence"] is None for item in refreshed["requirements"])


@pytest.mark.parametrize("level", ["primary", "secondary", "tertiary"])
async def test_expiry_at_exact_instant_blocks_all_dlc_levels(
    db_session, ready, clock, level
):
    if level == "primary":
        ready.batch.expires_at = NOW
    else:
        ready.batch.status = "opened"
        ready.batch.opened_at = NOW - timedelta(hours=2)
        ready.batch.use_within_hours_after_opening = 2 if level == "secondary" else 10
        if level == "tertiary":
            ready.batch.tertiary_started_at = NOW - timedelta(hours=1)
            ready.batch.tertiary_use_within_hours = 1
    await db_session.flush()
    row, report = await prove(db_session, ready)
    assert row.status == "in_progress"
    assert "HACCP_EXPIRED_BATCH_NOT_EXCLUDED" in codes(report)


async def test_expired_excluded_lot_and_traceable_replacement_are_allowed(
    db_session, ready
):
    ready.batch.expires_at = NOW
    (
        ready.position.excluded_at,
        ready.position.excluded_by,
        ready.position.exclusion_reason,
    ) = NOW, 7, "Écarté"
    replacement = IngredientBatch(
        ingredient_id=ready.ingredient.id,
        quantity=1,
        received_at=NOW,
        expires_at=NOW + timedelta(days=2),
    )
    db_session.add(replacement)
    await db_session.flush()
    db_session.add(
        StockBatchPosition(
            batch_id=replacement.id,
            establishment_id=ready.site.id,
            ingredient_id=ready.ingredient.id,
            remaining_quantity=1,
        )
    )
    ready.stock.physical_quantity = 3
    await db_session.flush()
    row, report = await prove(db_session, ready)
    assert row.status == "complete"


async def test_empty_stock_is_visible_and_requires_confirmation(db_session, ready):
    ready.stock.physical_quantity = ready.position.remaining_quantity = 0
    ready.batch.expires_at = None
    await db_session.flush()
    initial = await service.start(db_session, ready.site, "opening", ADMIN)
    dlc = next(item for item in initial["requirements"] if item["kind"] == "dlc")
    assert dlc["snapshot"]["physical_quantity"] == "0"
    row, report = await prove(db_session, ready)
    assert row.status == "complete"


@pytest.mark.parametrize(
    "problem", ["unreconciled", "missing_dlc", "missing_limits", "unknown_quantity"]
)
async def test_configuration_holes_never_validate(db_session, ready, problem):
    if problem == "unreconciled":
        ready.stock.reconciled_at = ready.stock.reconciled_by = None
    elif problem == "missing_dlc":
        ready.batch.expires_at = None
    elif problem == "missing_limits":
        ready.equipment.target_min_temp = ready.equipment.target_max_temp = None
    else:
        ready.position.remaining_quantity = None
    await db_session.flush()
    row, report = await prove(db_session, ready)
    assert row.status == "in_progress" and report["blockers"]


async def test_disabling_bad_equipment_does_not_erase_noncompliance(db_session, ready):
    report = await service.start(db_session, ready.site, "opening", ADMIN)
    row = await service.get_check(db_session, ready.site.id, report["id"])
    await service.record(
        db_session, ready.site, row, evidence(report, temperature=9), ADMIN
    )
    ready.equipment.is_active = False
    await db_session.flush()
    refreshed = await service.start(db_session, ready.site, "opening", ADMIN)
    assert "HACCP_UNRESOLVED_NONCOMPLIANCE" in codes(refreshed)


async def test_missed_closing_regularized_now_without_fabricating_checks(
    db_session, ready, clock
):
    yesterday = NOW.date() - timedelta(
        days=2
    )  # A day off in between creates no extra obligation.
    db_session.add(
        h.HaccpDailySession(
            establishment_id=ready.site.id,
            business_date=yesterday,
            timezone="UTC",
            session_type="opening",
            status="complete",
            validated_by=7,
            validated_at=NOW - timedelta(days=2),
        )
    )
    await db_session.flush()
    initial = await service.start(db_session, ready.site, "opening", ADMIN)
    assert "HACCP_PREVIOUS_CLOSING_MISSING" in codes(initial)
    missing = await service.prepare_regularization(db_session, ready.site, ADMIN)
    assert len(missing["missing_closings"]) == 1
    row = await service.get_check(
        db_session, ready.site.id, missing["missing_closings"][0]["id"]
    )
    body = ExplainRequest(
        expected_revision=1,
        idempotency_key="reg",
        reason="Oubli",
        actions_taken="Vérifications réalisées maintenant",
    )
    result = await service.regularize(db_session, ready.site, row, body, ADMIN)
    assert result["recorded_at"] == NOW.isoformat() and result["opening_still_required"]
    assert not (await requirements_for(db_session, row))
    assert "HACCP_OPENING_MISSING" in codes(
        await service.decision(db_session, ready.site)
    )
    assert await service.regularize(db_session, ready.site, row, body, ADMIN) == result


async def requirements_for(session, row):
    return await service.requirements(session, row)


async def test_sessions_before_activation_do_not_create_closing_debt(db_session, ready):
    db_session.add(
        h.HaccpDailySession(
            establishment_id=ready.site.id,
            business_date=date(2020, 1, 1),
            timezone="UTC",
            session_type="opening",
            status="complete",
            validated_by=7,
            validated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.flush()
    assert not await service.missing_closings(db_session, ready.site.id, NOW.date())


async def test_reopening_resets_both_checks_and_preserves_audit(db_session, ready):
    row, _ = await prove(db_session, ready)
    closing, _ = await prove(db_session, ready, "closing")
    body = ExplainRequest(
        expected_revision=1,
        idempotency_key="reopen",
        reason="Service exceptionnel",
        actions_taken="Reprise des contrôles",
    )
    report = await service.reopen(db_session, ready.site, row, body, ADMIN)
    assert row.revision == closing.revision == 2
    assert row.status == closing.status == "in_progress"
    assert not report["can_validate"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(h.HaccpAuditEvent)
            .where(h.HaccpAuditEvent.action == "revalidation_required")
        )
        == 2
    )


async def test_after_midnight_closing_then_late_regularization(
    db_session, ready, clock
):
    await prove(db_session, ready)
    clock.now = NOW.replace(hour=0) + timedelta(days=1, hours=1)
    report = await service.start(db_session, ready.site, "closing", ADMIN)
    assert report["business_date"] == NOW.date().isoformat()
    clock.now += timedelta(hours=4)
    row = await service.get_check(db_session, ready.site.id, report["id"])
    with pytest.raises(AppError) as error:
        await service.record(db_session, ready.site, row, evidence(report), ADMIN)
    assert error.value.code == "HACCP_CONTROL_DATE_INVALID"


@pytest.mark.parametrize(
    "frequency,anchor,day,expected",
    [
        ("weekly", date(2026, 9, 28), date(2026, 10, 4), date(2026, 9, 28)),
        ("monthly", date(2026, 1, 31), date(2026, 2, 28), date(2026, 2, 28)),
        ("monthly", date(2026, 1, 31), date(2026, 3, 1), date(2026, 2, 28)),
        ("weekly", date(2026, 10, 10), date(2026, 10, 4), None),
    ],
)
def test_due_dates_include_overdue_current_period(frequency, anchor, day, expected):
    assert cycle_start(day, anchor, frequency) == expected


@pytest.fixture
def api_user(monkeypatch, db_session):
    user = dict(ADMIN)
    app.dependency_overrides[get_current_user] = lambda: user

    @asynccontextmanager
    async def tenant_session(_slug):
        yield db_session

    monkeypatch.setattr(strict_router, "get_tenant_session", tenant_session)
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    "role,permissions,expected",
    [
        ("customer", ["*"], 403),
        ("staff", [], 403),
        ("staff", ["haccp:write"], 403),
        ("admin", [], 200),
    ],
)
async def test_api_roles_and_establishment_scope(
    client, ready, api_user, role, permissions, expected
):
    api_user.update(role=role, permissions=permissions)
    response = await client.post(
        f"/api/v1/haccp/strict/establishments/{ready.site.id}/sessions",
        json={"session_type": "opening"},
    )
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "injected", [{"force": True}, {"business_date": "2020-01-01"}, {"recorded_by": 1}]
)
async def test_api_rejects_client_authority(client, ready, api_user, injected):
    response = await client.post(
        f"/api/v1/haccp/strict/establishments/{ready.site.id}/sessions",
        json={"session_type": "opening", **injected},
    )
    assert response.status_code == 422


async def test_api_habilitated_staff_and_cross_site_session(
    client, db_session, ready, api_user, bootstrap_default_tenant
):
    api_user.update(
        id=bootstrap_default_tenant["staff_user_id"],
        role="staff",
        permissions=["haccp:read", "haccp:write", "haccp:validate"],
    )
    db_session.add(
        h.HaccpStaffScope(
            establishment_id=ready.site.id, user_id=api_user["id"], assigned_by=7
        )
    )
    await db_session.flush()
    url = f"/api/v1/haccp/strict/establishments/{ready.site.id}"
    result = await client.post(url + "/sessions", json={"session_type": "opening"})
    assert result.status_code == 200, result.text
    session_id = result.json()["id"]
    other = Establishment(name="B", timezone="UTC")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        h.HaccpStaffScope(
            establishment_id=other.id, user_id=api_user["id"], assigned_by=7
        )
    )
    await db_session.flush()
    response = await client.get(
        f"/api/v1/haccp/strict/establishments/{other.id}/sessions/{session_id}"
    )
    assert response.status_code == 404


async def test_api_admin_assigns_and_revokes_site_habilitation(
    client, db_session, ready, api_user, bootstrap_default_tenant
):
    staff_id = bootstrap_default_tenant["staff_user_id"]
    url = f"/api/v1/haccp/strict/establishments/{ready.site.id}"
    response = await client.put(f"{url}/staff/{staff_id}")
    assert response.status_code == 200
    api_user.update(
        id=staff_id, role="staff", permissions=["haccp:read", "haccp:manage"]
    )
    assert (await client.get(url + "/status")).status_code == 200
    assert (await client.put(f"{url}/staff/{staff_id}")).status_code == 403
    api_user.update(ADMIN)
    assert (await client.delete(f"{url}/staff/{staff_id}")).status_code == 200
    api_user.update(id=staff_id, role="staff", permissions=["haccp:read"])
    assert (await client.get(url + "/status")).status_code == 403


async def test_preopening_on_previous_day_and_paris_timezone(db_session, ready, clock):
    ready.site.timezone = "Europe/Paris"
    slots = (
        await db_session.scalars(
            select(h.EstablishmentBusinessHours).where(
                h.EstablishmentBusinessHours.establishment_id == ready.site.id
            )
        )
    ).all()
    for slot in slots:
        slot.opens_at = time(0, 30)
    clock.now = datetime(
        2026, 10, 3, 21, 45, tzinfo=timezone.utc
    )  # 23:45 Paris, control for 00:30.
    await db_session.flush()
    report = await service.start(db_session, ready.site, "opening", ADMIN)
    assert report["business_date"] == "2026-10-04"
    assert not await service.daily(
        db_session, ready.site.id, date(2026, 10, 3), "opening"
    )


async def test_overdue_weekly_cleaning_and_unknown_schedule(db_session, ready):
    ready.task.frequency = "weekly"
    scope = await db_session.get(h.HaccpCleaningScope, ready.task.id)
    scope.due_from = date(2026, 9, 28)
    await db_session.flush()
    report = await service.start(db_session, ready.site, "opening", ADMIN)
    task = next(item for item in report["requirements"] if item["kind"] == "cleaning")
    assert task["snapshot"]["due"] == "2026-09-28"
    scope.due_from = None
    await db_session.flush()
    report = await service.start(db_session, ready.site, "opening", ADMIN)
    assert "HACCP_NOT_CONFIGURED" in codes(report)


@pytest.fixture
async def isolated_sites(db_engine, clock):
    """Committed fixtures for concurrency; never reuse operational tenant names."""
    slugs = ["hctest" + uuid.uuid4().hex[:16] for _ in range(2)]
    sites = []
    try:
        for slug in slugs:
            async with db_engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "tenant_{slug}"'))
                await _provision_tenant_schema(conn, slug)
            async with get_tenant_session(slug) as session:
                ready = await seed_ready(session)
                sites.append(ready.site.id)
                await session.commit()
        yield list(zip(slugs, sites))
    finally:
        for slug in slugs:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(f'DROP SCHEMA IF EXISTS "tenant_{slug}" CASCADE')
                )


async def test_two_tenants_with_same_ids_do_not_share_approvals(isolated_sites):
    (slug_a, site_a), (slug_b, site_b) = isolated_sites
    assert site_a == site_b
    actor = {**ADMIN, "tenant_slug": slug_a}
    async with strict_router.transaction(actor, site_a, "haccp:write") as (
        session,
        site,
    ):
        await prove(session, SimpleNamespace(site=site))
    async with strict_router.transaction(
        {**actor, "tenant_slug": slug_b}, site_b, "haccp:read"
    ) as (session, site):
        result = await service.decision(session, site)
        assert not result["daily_haccp_satisfied"]
        assert (
            await session.scalar(select(func.count()).select_from(h.HaccpEvidence)) == 0
        )


async def test_concurrent_start_and_confirmation_leave_single_proof_set(isolated_sites):
    slug, site_id = isolated_sites[0]
    actor = {**ADMIN, "tenant_slug": slug}

    async def start():
        async with strict_router.transaction(actor, site_id, "haccp:write") as (
            session,
            site,
        ):
            return await service.start(session, site, "opening", actor)

    results = await asyncio.gather(start(), start(), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            assert (
                isinstance(result, AppError) and result.code == "HACCP_RETRY_REQUIRED"
            )
    initial = await start()
    body = evidence(initial, "concurrent-evidence")

    async def confirm():
        async with strict_router.transaction(actor, site_id, "haccp:write") as (
            session,
            site,
        ):
            row = await service.get_check(session, site_id, initial["id"])
            return await service.record(session, site, row, body, actor)

    results = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            assert (
                isinstance(result, AppError) and result.code == "HACCP_RETRY_REQUIRED"
            )
    await confirm()  # Safe replay after response loss or serialization conflict.
    async with get_tenant_session(slug) as session:
        assert (
            await session.scalar(select(func.count()).select_from(h.HaccpDailySession))
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(h.HaccpEvidence)) == 3
        )
