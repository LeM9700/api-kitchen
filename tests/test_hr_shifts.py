"""Tests for HR shift (planning) endpoints."""

import pytest
from pydantic import ValidationError


async def test_create_shift_requires_admin_role(client):
    resp = await client.post(
        "/api/v1/hr/shifts",
        json={
            "employee_id": 1,
            "establishment_id": 1,
            "starts_at": "2026-08-01T08:00:00Z",
            "ends_at": "2026-08-01T16:00:00Z",
        },
    )
    assert resp.status_code in (401, 403)


async def test_list_shifts_requires_admin_role(client):
    resp = await client.get(
        "/api/v1/hr/shifts",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)


async def test_my_shifts_requires_auth(client):
    resp = await client.get("/api/v1/hr/shifts/me")
    assert resp.status_code in (401, 403)


def test_shift_create_rejects_ends_before_starts():
    from app.modules.hr.schemas import ShiftCreate

    with pytest.raises(ValidationError):
        ShiftCreate(
            employee_id=1,
            establishment_id=1,
            starts_at="2026-08-01T16:00:00Z",
            ends_at="2026-08-01T08:00:00Z",
        )


@pytest.fixture
async def default_establishment_id(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await db_session.execute(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
    establishment_id = result.scalar_one()
    result.close()
    return establishment_id


async def test_create_and_list_shift(db_session, default_establishment_id):
    from app.modules.hr.schemas import EmployeeProfileCreate, ShiftCreate
    from app.modules.hr.service import create_employee_profile, create_shift, list_shifts

    employee = await create_employee_profile(
        db_session,
        EmployeeProfileCreate(
            user_id=99002,
            establishment_id=default_establishment_id,
        ),
    )
    shift = await create_shift(
        db_session,
        ShiftCreate(
            employee_id=employee.id,
            establishment_id=default_establishment_id,
            starts_at="2026-08-01T08:00:00Z",
            ends_at="2026-08-01T16:00:00Z",
        ),
        created_by_user_id=1,
    )
    assert shift.id is not None

    shifts = await list_shifts(db_session, employee_id=employee.id)
    assert any(item.id == shift.id for item in shifts)
