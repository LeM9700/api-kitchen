"""Quarter-hour HACCP opening reminders, scoped to establishments."""

import logging
from datetime import UTC, date, datetime, time, timedelta

import pytz
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.database import engine, get_tenant_session
from app.modules.auth.models import User
from app.modules.haccp import foundation_models as h
from app.modules.haccp.strict_service import decision
from app.modules.hr.models import Establishment
from app.modules.notifications.notification_service import notify_user
from worker.tasks.stats import _get_all_tenant_slugs

logger = logging.getLogger(__name__)

REMINDER_OFFSETS = (60, 45, 30, 15, 0)


def opening_due_times(opening: datetime) -> tuple[datetime, ...]:
    """Return H-60 through H in UTC, preserving the site's wall-clock time."""
    return tuple(opening - timedelta(minutes=offset) for offset in REMINDER_OFFSETS)


def _local_opening(
    site: Establishment, business_date: date, opening_time: time
) -> datetime:
    zone = pytz.timezone(site.timezone)
    return zone.localize(
        datetime.combine(business_date, opening_time), is_dst=None
    ).astimezone(UTC)


async def _recipients(session, establishment_id: int) -> list[int]:
    scope = h.HaccpStaffScope
    result = await session.scalars(
        select(User.id)
        .outerjoin(
            scope,
            and_(scope.user_id == User.id, scope.establishment_id == establishment_id),
        )
        .where(
            User.is_active.is_(True),
            or_(
                User.role == "admin",
                and_(User.role == "staff", scope.user_id.is_not(None)),
            ),
        )
        .order_by(User.id)
    )
    return list(result)


async def schedule_haccp_opening_reminders(ctx, *, now: datetime | None = None) -> None:
    """Create and dispatch due reminders without duplicating logical deliveries."""
    checked_at = now or datetime.now(UTC)
    redis = ctx.get("redis") if isinstance(ctx, dict) else None
    for tenant_slug in await _get_all_tenant_slugs(engine):
        try:
            async with get_tenant_session(tenant_slug) as session:
                sites = (
                    await session.scalars(
                        select(Establishment).where(Establishment.is_active.is_(True))
                    )
                ).all()
                for site in sites:
                    config = await session.get(h.EstablishmentOperations, site.id)
                    if config is None or not config.strict_enabled:
                        continue
                    local = checked_at.astimezone(pytz.timezone(site.timezone))
                    business_date = local.date()
                    slots = (
                        await session.scalars(
                            select(h.EstablishmentBusinessHours)
                            .where(
                                h.EstablishmentBusinessHours.establishment_id
                                == site.id,
                                h.EstablishmentBusinessHours.day_of_week
                                == business_date.weekday(),
                                h.EstablishmentBusinessHours.is_active.is_(True),
                            )
                            .order_by(h.EstablishmentBusinessHours.opens_at)
                        )
                    ).all()
                    if not slots:
                        continue
                    opening = _local_opening(site, business_date, slots[0].opens_at)
                    due = [
                        item
                        for item in opening_due_times(opening)
                        if item <= checked_at
                    ]
                    if not due:
                        continue
                    latest_due = due[-1]
                    # A worker outage must not create a burst of obsolete reminders.
                    if checked_at - latest_due > timedelta(minutes=20):
                        continue
                    state = await decision(session, site)
                    recipients = await _recipients(session, site.id)
                    for due_at in (latest_due,):
                        if state["daily_haccp_satisfied"]:
                            continue
                        for user_id in recipients:
                            reminder = h.HaccpReminderDelivery(
                                establishment_id=site.id,
                                business_date=business_date,
                                schedule_revision=config.revision,
                                due_at=due_at,
                                user_id=user_id,
                                channel="push",
                                status="pending",
                            )
                            try:
                                async with session.begin_nested():
                                    session.add(reminder)
                                    await session.flush()
                            except IntegrityError:
                                continue
                            try:
                                reminder.status = "sending"
                                reminder.attempts += 1
                                await notify_user(
                                    session=session,
                                    tenant_slug=tenant_slug,
                                    user_id=user_id,
                                    event="haccp.opening_reminder",
                                    title="Contrôle HACCP requis avant ouverture",
                                    body="Le contrôle HACCP de votre établissement n'est pas complet. Ouvrir le parcours de régularisation.",
                                    data={
                                        "establishment_id": site.id,
                                        "business_date": business_date.isoformat(),
                                        "due_at": due_at.isoformat(),
                                    },
                                    redis=redis,
                                )
                                reminder.status = "sent"
                                reminder.sent_at = checked_at
                            except (
                                Exception
                            ) as exc:  # pragma: no cover - provider boundary
                                reminder.status = "failed"
                                reminder.last_error = str(exc)[:1000]
                                logger.warning(
                                    "HACCP reminder failed tenant=%s site=%s user=%s: %s",
                                    tenant_slug,
                                    site.id,
                                    user_id,
                                    exc,
                                )
        except Exception:
            logger.exception("HACCP opening reminder tenant failed: %s", tenant_slug)
