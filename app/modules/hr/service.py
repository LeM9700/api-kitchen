"""Logique metier du module RH."""

from datetime import datetime as _datetime
from datetime import timezone as _timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.hr.models import EmployeeProfile, Shift, TimeClockEntry
from app.modules.hr.schemas import (
    ClockInRequest,
    EmployeeProfileCreate,
    EmployeeProfileUpdate,
    ShiftCreate,
    ShiftUpdate,
)


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


async def create_shift(
    session: AsyncSession,
    body: ShiftCreate,
    created_by_user_id: int,
) -> Shift:
    shift = Shift(
        employee_id=body.employee_id,
        establishment_id=body.establishment_id,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        break_minutes=body.break_minutes,
        created_by_user_id=created_by_user_id,
    )
    session.add(shift)
    await session.commit()
    await session.refresh(shift)
    return shift


async def update_shift(
    session: AsyncSession,
    shift_id: int,
    body: ShiftUpdate,
) -> Shift:
    shift = await session.get(Shift, shift_id)
    if shift is None:
        raise AppError("NOT_FOUND", "Shift not found", 404)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(shift, field, value)

    await session.commit()
    await session.refresh(shift)
    return shift


async def list_shifts(
    session: AsyncSession,
    employee_id: int | None = None,
    date_from: _datetime | None = None,
    date_to: _datetime | None = None,
) -> list[Shift]:
    stmt = select(Shift)
    if employee_id is not None:
        stmt = stmt.where(Shift.employee_id == employee_id)
    if date_from is not None:
        stmt = stmt.where(Shift.starts_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Shift.starts_at <= date_to)

    result = await session.execute(stmt.order_by(Shift.starts_at))
    return list(result.scalars())


async def list_my_shifts(
    session: AsyncSession,
    employee_id: int,
    date_from: _datetime | None = None,
    date_to: _datetime | None = None,
) -> list[Shift]:
    return await list_shifts(
        session,
        employee_id=employee_id,
        date_from=date_from,
        date_to=date_to,
    )


async def clock_in(
    session: AsyncSession,
    employee_id: int,
    body: ClockInRequest,
) -> TimeClockEntry:
    existing = await session.execute(
        select(TimeClockEntry).where(
            TimeClockEntry.employee_id == employee_id,
            TimeClockEntry.status == "open",
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise AppError(
            "ALREADY_CLOCKED_IN",
            "Employee already has an open time clock entry",
            409,
        )

    entry = TimeClockEntry(
        employee_id=employee_id,
        shift_id=body.shift_id,
        establishment_id=body.establishment_id,
        clock_in_at=_datetime.now(_timezone.utc),
        method=body.method,
        status="open",
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def clock_out(
    session: AsyncSession,
    employee_id: int,
) -> TimeClockEntry:
    result = await session.execute(
        select(TimeClockEntry).where(
            TimeClockEntry.employee_id == employee_id,
            TimeClockEntry.status == "open",
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise AppError("NOT_CLOCKED_IN", "No open time clock entry for this employee", 409)

    entry.clock_out_at = _datetime.now(_timezone.utc)
    entry.status = "closed"
    await session.commit()
    await session.refresh(entry)
    return entry
