"""Logique metier du module RH."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.hr.models import EmployeeProfile
from app.modules.hr.schemas import EmployeeProfileCreate, EmployeeProfileUpdate


async def create_employee_profile(
    session: AsyncSession,
    body: EmployeeProfileCreate,
) -> EmployeeProfile:
    profile = EmployeeProfile(
        user_id=body.user_id,
        establishment_id=body.establishment_id,
        hourly_rate_cents=body.hourly_rate_cents,
        weekly_hours_contract=body.weekly_hours_contract,
        hire_date=body.hire_date,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def list_employee_profiles(session: AsyncSession) -> list[EmployeeProfile]:
    result = await session.execute(select(EmployeeProfile).order_by(EmployeeProfile.id))
    return list(result.scalars())


async def get_employee_profile_by_user_id(
    session: AsyncSession,
    user_id: int,
) -> EmployeeProfile:
    result = await session.execute(
        select(EmployeeProfile).where(EmployeeProfile.user_id == user_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise AppError("NOT_FOUND", "Employee profile not found", 404)
    return profile


async def update_employee_profile(
    session: AsyncSession,
    employee_id: int,
    body: EmployeeProfileUpdate,
) -> EmployeeProfile:
    profile = await session.get(EmployeeProfile, employee_id)
    if profile is None:
        raise AppError("NOT_FOUND", "Employee profile not found", 404)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

    await session.commit()
    await session.refresh(profile)
    return profile
