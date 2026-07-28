"""Tests for HR time clock listing and admin corrections."""

import pytest


async def test_list_all_entries_requires_admin(client):
    resp = await client.get(
        "/api/v1/hr/timeclock/entries",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)


async def test_my_entries_requires_auth(client):
    resp = await client.get("/api/v1/hr/timeclock/entries/me")
    assert resp.status_code in (401, 403)


async def test_correct_entry_requires_admin(client):
    resp = await client.patch(
        "/api/v1/hr/timeclock/entries/1",
        json={
            "new_clock_in_at": "2026-08-01T08:05:00Z",
            "reason": "oubli de pointage",
        },
    )
    assert resp.status_code in (401, 403)


@pytest.fixture
async def default_establishment_id(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await db_session.execute(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
    establishment_id = result.scalar_one()
    result.close()
    return establishment_id


@pytest.fixture
async def closed_entry(db_session, default_establishment_id):
    from app.modules.hr.schemas import ClockInRequest, EmployeeProfileCreate
    from app.modules.hr.service import clock_in, clock_out, create_employee_profile

    employee = await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99004,
            establishment_id=default_establishment_id,
        ),
    )
    await clock_in(
        db_session,
        employee.id,
        ClockInRequest(method="web", establishment_id=default_establishment_id),
    )
    entry = await clock_out(db_session, employee.id)
    return employee, entry


async def test_list_my_time_clock_entries_scoped_to_employee(db_session, closed_entry):
    from app.modules.hr.service import list_my_time_clock_entries

    employee, entry = closed_entry
    entries = await list_my_time_clock_entries(db_session, employee.id)
    assert [item.id for item in entries] == [entry.id]


async def test_correct_time_clock_entry_records_audit_trail(db_session, closed_entry):
    import sqlalchemy as sa

    from app.modules.hr.schemas import TimeClockCorrectionRequest
    from app.modules.hr.service import correct_time_clock_entry

    _, entry = closed_entry
    assert entry.clock_out_at is not None
    new_clock_out = entry.clock_out_at.replace(minute=0)

    corrected = await correct_time_clock_entry(
        db_session,
        entry.id,
        TimeClockCorrectionRequest(
            new_clock_out_at=new_clock_out,
            reason="oubli de pointage",
        ),
        corrected_by_user_id=1,
    )
    assert corrected.clock_out_at == new_clock_out
    assert corrected.status == "corrected"

    result = await db_session.execute(
        sa.text("SELECT reason FROM time_clock_corrections WHERE entry_id = :id"),
        {"id": entry.id},
    )
    assert result.scalar_one() == "oubli de pointage"
    result.close()
