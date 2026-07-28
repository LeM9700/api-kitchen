"""Tests for HR time clock (pointage) endpoints."""

import pytest


async def test_clock_in_requires_auth(client):
    resp = await client.post(
        "/api/v1/hr/timeclock/clock-in",
        json={"method": "web", "establishment_id": 1},
    )
    assert resp.status_code in (401, 403)


async def test_clock_out_requires_auth(client):
    resp = await client.post("/api/v1/hr/timeclock/clock-out")
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
async def employee(db_session, default_establishment_id):
    from app.modules.hr.schemas import EmployeeProfileCreate
    from app.modules.hr.service import create_employee_profile

    return await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99003,
            establishment_id=default_establishment_id,
        ),
    )


async def test_clock_in_then_clock_in_again_raises_409(
    db_session,
    employee,
    default_establishment_id,
):
    from app.core.http.errors import AppError
    from app.modules.hr.schemas import ClockInRequest
    from app.modules.hr.service import clock_in

    body = ClockInRequest(method="web", establishment_id=default_establishment_id)
    entry = await clock_in(db_session, employee.id, body)
    assert entry.status == "open"

    with pytest.raises(AppError) as exc_info:
        await clock_in(db_session, employee.id, body)
    assert exc_info.value.status_code == 409


async def test_clock_out_without_open_entry_raises_409(db_session, employee):
    from app.core.http.errors import AppError
    from app.modules.hr.service import clock_out

    with pytest.raises(AppError) as exc_info:
        await clock_out(db_session, employee.id)
    assert exc_info.value.status_code == 409


async def test_clock_in_then_clock_out_closes_entry(
    db_session,
    employee,
    default_establishment_id,
):
    from app.modules.hr.schemas import ClockInRequest
    from app.modules.hr.service import clock_in, clock_out

    await clock_in(
        db_session,
        employee.id,
        ClockInRequest(
            method="qrcode",
            establishment_id=default_establishment_id,
        ),
    )
    entry = await clock_out(db_session, employee.id)
    assert entry.status == "closed"
    assert entry.clock_out_at is not None
