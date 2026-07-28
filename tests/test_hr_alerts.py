"""Tests for HR alert detection logic."""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def default_establishment_id(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await db_session.execute(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
    establishment_id = result.scalar_one()
    result.close()
    return establishment_id


@pytest.fixture
async def employee_with_shift(db_session, default_establishment_id):
    from app.modules.hr.schemas import EmployeeProfileCreate, ShiftCreate
    from app.modules.hr.service import create_employee_profile, create_shift

    employee = await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99005,
            establishment_id=default_establishment_id,
        ),
    )
    starts_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=20)
    shift = await create_shift(
        db_session,
        ShiftCreate(
            employee_id=employee.id,
            establishment_id=default_establishment_id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=8),
        ),
        created_by_user_id=1,
    )
    return employee, shift


async def test_detect_late_arrival_beyond_tolerance(db_session, employee_with_shift):
    from app.modules.hr.models import TimeClockEntry
    from app.modules.hr.service import detect_late_arrival

    employee, shift = employee_with_shift
    entry = TimeClockEntry(
        employee_id=employee.id,
        shift_id=shift.id,
        establishment_id=shift.establishment_id,
        clock_in_at=shift.starts_at + timedelta(minutes=20),
        method="web",
        status="open",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    result = await detect_late_arrival(db_session, entry)
    assert result is not None
    assert result["employee_id"] == employee.id
    assert result["minutes_late"] >= 10


async def test_detect_late_arrival_within_tolerance_returns_none(
    db_session,
    employee_with_shift,
):
    from app.modules.hr.models import TimeClockEntry
    from app.modules.hr.service import detect_late_arrival

    employee, shift = employee_with_shift
    entry = TimeClockEntry(
        employee_id=employee.id,
        shift_id=shift.id,
        establishment_id=shift.establishment_id,
        clock_in_at=shift.starts_at + timedelta(minutes=5),
        method="web",
        status="open",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    assert await detect_late_arrival(db_session, entry) is None


async def test_detect_late_arrival_without_shift_returns_none(
    db_session,
    employee_with_shift,
):
    from app.modules.hr.models import TimeClockEntry
    from app.modules.hr.service import detect_late_arrival

    employee, shift = employee_with_shift
    entry = TimeClockEntry(
        employee_id=employee.id,
        shift_id=None,
        establishment_id=shift.establishment_id,
        clock_in_at=datetime.now(timezone.utc),
        method="web",
        status="open",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    assert await detect_late_arrival(db_session, entry) is None


async def test_detect_shift_overrun_past_end_plus_tolerance(
    db_session,
    employee_with_shift,
):
    from app.modules.hr.models import TimeClockEntry
    from app.modules.hr.service import detect_shift_overrun

    employee, shift = employee_with_shift
    entry = TimeClockEntry(
        employee_id=employee.id,
        shift_id=shift.id,
        establishment_id=shift.establishment_id,
        clock_in_at=shift.starts_at,
        clock_out_at=shift.ends_at + timedelta(minutes=25),
        method="web",
        status="closed",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    result = await detect_shift_overrun(db_session, entry)
    assert result is not None
    assert result["minutes_over"] >= 15


async def test_list_alerts_requires_admin(client):
    resp = await client.get(
        "/api/v1/hr/alerts",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)


async def test_resolve_alert_requires_admin(client):
    resp = await client.patch("/api/v1/hr/alerts/1/resolve")
    assert resp.status_code in (401, 403)


async def test_list_and_resolve_alert(db_session, employee_with_shift):
    from app.modules.hr.models import HrAlert
    from app.modules.hr.service import list_alerts, resolve_alert

    employee, _shift = employee_with_shift
    db_session.add(
        HrAlert(
            employee_id=employee.id,
            establishment_id=employee.establishment_id,
            type="late",
            payload={"minutes_late": 12},
        )
    )
    await db_session.commit()

    alerts = await list_alerts(db_session, resolved=False)
    assert len(alerts) == 1
    assert alerts[0].employee_id == employee.id

    resolved = await resolve_alert(db_session, alerts[0].id)
    assert resolved.resolved_at is not None

    still_open = await list_alerts(db_session, resolved=False)
    assert len(still_open) == 0
