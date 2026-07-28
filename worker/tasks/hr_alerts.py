"""Tasks ARQ pour les alertes RH."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.hr.models import (
    EmployeeProfile,
    EstablishmentHrConfig,
    HrAlert,
    TimeClockEntry,
)
from worker.tasks.stats import _get_all_tenant_slugs

try:
    from app.modules.notifications.notification_service import notify_staff
except Exception:  # pragma: no cover - defensive import for worker bootstrap/tests
    notify_staff = None

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_HOURS = 4


@asynccontextmanager
async def _open_tenant_session(tenant_slug: str):
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)
    try:
        async with session_factory() as session:
            await session.execute(text(f'SET search_path TO "{schema}", public'))
            yield session
    finally:
        await engine.dispose()


async def _establishment_id_for_employee(session, employee_id: int) -> int | None:
    result = await session.execute(
        select(EmployeeProfile.establishment_id).where(EmployeeProfile.id == employee_id)
    )
    establishment_id = result.scalar_one_or_none()
    result.close()
    return establishment_id


async def _cooldown_hours(
    session,
    employee_id: int,
    establishment_id: int | None = None,
) -> int:
    if establishment_id is None:
        establishment_id = await _establishment_id_for_employee(session, employee_id)
    if establishment_id is None:
        return _DEFAULT_COOLDOWN_HOURS

    result = await session.execute(
        select(EstablishmentHrConfig).where(
            EstablishmentHrConfig.establishment_id == establishment_id
        )
    )
    config = result.scalar_one_or_none()
    result.close()
    return int(config.alert_cooldown_hours) if config else _DEFAULT_COOLDOWN_HOURS


async def _record_alert_if_not_in_cooldown(
    session,
    employee_id: int,
    alert_type: str,
    severity: str,
    payload: dict,
) -> HrAlert | None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(HrAlert)
        .where(HrAlert.employee_id == employee_id, HrAlert.type == alert_type)
        .order_by(HrAlert.id.desc())
        .limit(1)
    )
    last_alert = result.scalar_one_or_none()
    result.close()

    cooldown_hours = await _cooldown_hours(
        session,
        employee_id,
        payload.get("establishment_id"),
    )
    if last_alert and last_alert.last_alert_sent_at:
        cooldown_until = last_alert.last_alert_sent_at + timedelta(hours=cooldown_hours)
        if now < cooldown_until:
            logger.info(
                "HR alert skipped by cooldown=%sh type=%s employee=%s",
                cooldown_hours,
                alert_type,
                employee_id,
            )
            return None

    alert = HrAlert(
        employee_id=employee_id,
        type=alert_type,
        severity=severity,
        payload=payload,
        last_alert_sent_at=now,
    )
    session.add(alert)
    await session.commit()
    return alert


async def send_hr_late_alert(
    ctx,
    tenant_slug: str,
    employee_id: int,
    shift_id: int,
    minutes_late: int,
) -> None:
    async with _open_tenant_session(tenant_slug) as session:
        alert = await _record_alert_if_not_in_cooldown(
            session,
            employee_id,
            "late",
            "warning",
            {"shift_id": shift_id, "minutes_late": minutes_late},
        )
        if alert is None:
            return

        try:
            if notify_staff is not None:
                await notify_staff(
                    session=session,
                    tenant_slug=tenant_slug,
                    event="hr.late_arrival",
                    title="Retard employe",
                    body=f"Employe #{employee_id} : {minutes_late} min de retard.",
                    data={
                        "employee_id": employee_id,
                        "shift_id": shift_id,
                        "minutes_late": minutes_late,
                    },
                )
        except Exception as exc:
            logger.error(
                "notify_staff failed for hr.late_arrival employee=%s: %s",
                employee_id,
                exc,
            )


async def send_hr_overrun_alert(
    ctx,
    tenant_slug: str,
    employee_id: int,
    shift_id: int,
    minutes_over: int,
) -> None:
    async with _open_tenant_session(tenant_slug) as session:
        alert = await _record_alert_if_not_in_cooldown(
            session,
            employee_id,
            "shift_overrun",
            "warning",
            {"shift_id": shift_id, "minutes_over": minutes_over},
        )
        if alert is None:
            return

        try:
            if notify_staff is not None:
                await notify_staff(
                    session=session,
                    tenant_slug=tenant_slug,
                    event="hr.shift_overrun",
                    title="Depassement de shift",
                    body=f"Employe #{employee_id} : {minutes_over} min au-dela du shift prevu.",
                    data={
                        "employee_id": employee_id,
                        "shift_id": shift_id,
                        "minutes_over": minutes_over,
                    },
                )
        except Exception as exc:
            logger.error(
                "notify_staff failed for hr.shift_overrun employee=%s: %s",
                employee_id,
                exc,
            )


async def _get_config_for_employee(
    session,
    employee: EmployeeProfile,
) -> EstablishmentHrConfig:
    result = await session.execute(
        select(EstablishmentHrConfig).where(
            EstablishmentHrConfig.establishment_id == employee.establishment_id
        )
    )
    config = result.scalar_one_or_none()
    result.close()
    if config is None:
        return EstablishmentHrConfig(establishment_id=employee.establishment_id)
    return config


async def _weekly_hours_worked(session, employee_id: int, as_of: datetime) -> float:
    monday = (as_of - timedelta(days=as_of.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    result = await session.execute(
        select(TimeClockEntry).where(
            TimeClockEntry.employee_id == employee_id,
            TimeClockEntry.status == "closed",
            TimeClockEntry.clock_in_at >= monday,
        )
    )

    total_minutes = 0.0
    for entry in result.scalars():
        if entry.clock_out_at is None:
            continue
        worked = (entry.clock_out_at - entry.clock_in_at).total_seconds() / 60
        total_minutes += worked
    result.close()
    return round(total_minutes / 60, 2)


async def check_weekly_overtime(ctx) -> None:
    """Cron ARQ quotidien: alerte si un employe depasse le seuil hebdomadaire."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)
        for slug in tenant_slugs:
            schema = tenant_schema_name(slug)
            async with session_factory() as session:
                await session.execute(text(f'SET search_path TO "{schema}", public'))
                result = await session.execute(
                    select(EmployeeProfile).where(EmployeeProfile.is_active.is_(True))
                )
                employees = list(result.scalars())
                result.close()

                for employee in employees:
                    hours = await _weekly_hours_worked(session, employee.id, now)
                    config = await _get_config_for_employee(session, employee)
                    threshold = int(config.weekly_hours_legal_threshold)
                    if hours <= threshold:
                        continue

                    alert = await _record_alert_if_not_in_cooldown(
                        session,
                        employee.id,
                        "weekly_overtime",
                        "warning",
                        {
                            "hours_worked": hours,
                            "threshold": threshold,
                            "establishment_id": employee.establishment_id,
                        },
                    )
                    if alert is None:
                        continue

                    try:
                        if notify_staff is not None:
                            await notify_staff(
                                session=session,
                                tenant_slug=slug,
                                event="hr.weekly_overtime",
                                title="Depassement des 35h",
                                body=(
                                    f"Employe #{employee.id} : {hours}h cette semaine "
                                    f"(seuil {threshold}h)."
                                ),
                                data={
                                    "employee_id": employee.id,
                                    "hours_worked": hours,
                                    "threshold": threshold,
                                },
                            )
                    except Exception as exc:
                        logger.error(
                            "notify_staff failed for hr.weekly_overtime tenant=%s employee=%s: %s",
                            slug,
                            employee.id,
                            exc,
                        )
    finally:
        await engine.dispose()
