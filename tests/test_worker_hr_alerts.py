"""Tests for HR alert worker tasks: cooldown + HrAlert row creation."""

from contextlib import asynccontextmanager
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
async def employee(db_session, default_establishment_id):
    from app.modules.hr.schemas import EmployeeProfileCreate
    from app.modules.hr.service import create_employee_profile

    return await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99006,
            establishment_id=default_establishment_id,
        ),
    )


def _tenant_session_context(db_session):
    @asynccontextmanager
    async def _context(_tenant_slug: str):
        yield db_session

    return _context


async def test_send_hr_late_alert_creates_hr_alert_row(db_session, employee, monkeypatch):
    import sqlalchemy as sa

    from worker.tasks import hr_alerts

    monkeypatch.setattr(hr_alerts, "_open_tenant_session", _tenant_session_context(db_session))
    monkeypatch.setattr(hr_alerts, "notify_staff", None)

    await hr_alerts.send_hr_late_alert(
        {},
        tenant_slug="default",
        employee_id=employee.id,
        shift_id=1,
        minutes_late=15,
    )

    result = await db_session.execute(
        sa.text("SELECT type, severity FROM hr_alerts WHERE employee_id = :eid"),
        {"eid": employee.id},
    )
    alert = result.first()
    result.close()
    assert alert is not None
    assert alert.type == "late"
    assert alert.severity == "warning"


async def test_send_hr_late_alert_respects_cooldown(db_session, employee, monkeypatch):
    import sqlalchemy as sa

    from app.modules.hr.models import HrAlert
    from worker.tasks import hr_alerts

    db_session.add(
        HrAlert(
            employee_id=employee.id,
            type="late",
            severity="warning",
            payload={},
            last_alert_sent_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    monkeypatch.setattr(hr_alerts, "_open_tenant_session", _tenant_session_context(db_session))
    monkeypatch.setattr(hr_alerts, "notify_staff", None)

    await hr_alerts.send_hr_late_alert(
        {},
        tenant_slug="default",
        employee_id=employee.id,
        shift_id=1,
        minutes_late=15,
    )

    result = await db_session.execute(
        sa.text("SELECT COUNT(*) FROM hr_alerts WHERE employee_id = :eid AND type = 'late'"),
        {"eid": employee.id},
    )
    assert result.scalar_one() == 1
    result.close()


async def test_weekly_hours_worked_sums_closed_entries_this_week(
    db_session,
    employee,
    default_establishment_id,
):
    from app.modules.hr.models import TimeClockEntry
    from worker.tasks.hr_alerts import _weekly_hours_worked

    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    db_session.add(
        TimeClockEntry(
            employee_id=employee.id,
            establishment_id=default_establishment_id,
            clock_in_at=monday,
            clock_out_at=monday + timedelta(hours=10),
            method="web",
            status="closed",
        )
    )
    await db_session.commit()

    hours = await _weekly_hours_worked(db_session, employee.id, now)
    assert hours == pytest.approx(10.0, abs=0.01)


async def test_weekly_hours_worked_ignores_open_entries(
    db_session,
    employee,
    default_establishment_id,
):
    from app.modules.hr.models import TimeClockEntry
    from worker.tasks.hr_alerts import _weekly_hours_worked

    now = datetime.now(timezone.utc)
    db_session.add(
        TimeClockEntry(
            employee_id=employee.id,
            establishment_id=default_establishment_id,
            clock_in_at=now,
            clock_out_at=None,
            method="web",
            status="open",
        )
    )
    await db_session.commit()

    hours = await _weekly_hours_worked(db_session, employee.id, now)
    assert hours == 0.0


async def test_labor_cost_ratio_computed_from_hourly_rate_and_orders(
    db_session,
    default_establishment_id,
):
    from app.modules.hr.models import TimeClockEntry
    from app.modules.hr.schemas import EmployeeProfileCreate
    from app.modules.hr.service import create_employee_profile
    from app.modules.orders.models import Order
    from worker.tasks.hr_alerts import _labor_cost_ratio

    employee = await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99007,
            establishment_id=default_establishment_id,
            hourly_rate_cents=1500,
        ),
    )
    monday = datetime(2099, 1, 5, tzinfo=timezone.utc)

    db_session.add(
        TimeClockEntry(
            employee_id=employee.id,
            establishment_id=default_establishment_id,
            clock_in_at=monday,
            clock_out_at=monday + timedelta(hours=10),
            method="web",
            status="closed",
        )
    )
    db_session.add(
        Order(
            status="completed",
            payment_status="paid",
            total=100.00,
            created_at=monday,
        )
    )
    await db_session.commit()

    ratio = await _labor_cost_ratio(db_session, default_establishment_id, monday)
    assert ratio == pytest.approx(1.5, abs=0.01)


async def test_record_alert_allows_establishment_scope(
    db_session,
    default_establishment_id,
):
    from app.modules.hr.models import HrAlert
    from worker.tasks.hr_alerts import _record_alert_if_not_in_cooldown

    alert = await _record_alert_if_not_in_cooldown(
        db_session,
        employee_id=None,
        alert_type="labor_cost_risk",
        severity="critical",
        payload={"establishment_id": default_establishment_id, "ratio": 0.42},
        establishment_id=default_establishment_id,
    )

    assert isinstance(alert, HrAlert)
    assert alert.employee_id is None
    assert alert.establishment_id == default_establishment_id
    assert alert.type == "labor_cost_risk"
