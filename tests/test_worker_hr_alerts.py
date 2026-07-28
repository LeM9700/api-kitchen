"""Tests for HR alert worker tasks: cooldown + HrAlert row creation."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
