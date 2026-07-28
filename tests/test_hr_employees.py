"""Tests for HR employee profile endpoints."""

import pytest
from pydantic import ValidationError


async def test_list_employees_requires_admin_role(client):
    resp = await client.get(
        "/api/v1/hr/employees",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code in (401, 403)


async def test_create_employee_requires_admin_role(client):
    resp = await client.post(
        "/api/v1/hr/employees",
        json={"user_id": 1, "establishment_id": 1},
    )
    assert resp.status_code in (401, 403)


async def test_get_my_employee_profile_requires_auth(client):
    resp = await client.get("/api/v1/hr/employees/me")
    assert resp.status_code in (401, 403)


def test_employee_profile_create_requires_user_and_establishment():
    from app.modules.hr.schemas import EmployeeProfileCreate

    with pytest.raises(ValidationError):
        EmployeeProfileCreate()


def test_employee_profile_self_out_has_no_hourly_rate_field():
    from app.modules.hr.schemas import EmployeeProfileSelfOut

    assert "hourly_rate_cents" not in EmployeeProfileSelfOut.model_fields


def test_employee_profile_out_has_hourly_rate_field():
    from app.modules.hr.schemas import EmployeeProfileOut

    assert "hourly_rate_cents" in EmployeeProfileOut.model_fields


@pytest.fixture
async def default_establishment_id(db_session):
    import sqlalchemy as sa

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    result = await db_session.execute(sa.text("SELECT id FROM establishments ORDER BY id LIMIT 1"))
    establishment_id = result.scalar_one_or_none()
    result.close()
    assert establishment_id is not None, (
        "no establishment found; run the HR migration against the test database"
    )
    return establishment_id


async def test_create_and_list_employee_profile(db_session, default_establishment_id):
    from app.modules.hr.schemas import EmployeeProfileCreate
    from app.modules.hr.service import create_employee_profile, list_employee_profiles

    body = EmployeeProfileCreate(
        user_id=99001,
        establishment_id=default_establishment_id,
        hourly_rate_cents=1250,
        weekly_hours_contract=35,
    )
    created = await create_employee_profile(db_session, body)
    assert created.id is not None
    assert created.user_id == 99001

    profiles = await list_employee_profiles(db_session)
    assert any(profile.user_id == 99001 for profile in profiles)


async def test_get_employee_profile_by_user_id_not_found_raises(
    db_session,
    default_establishment_id,
):
    from app.core.http.errors import AppError
    from app.modules.hr.service import get_employee_profile_by_user_id

    assert default_establishment_id is not None
    with pytest.raises(AppError) as exc_info:
        await get_employee_profile_by_user_id(db_session, user_id=999999)
    assert exc_info.value.status_code == 404
