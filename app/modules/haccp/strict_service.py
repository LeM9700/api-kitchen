"""Strict daily workflow. Caller commits atomically; never activates ordering.

Lock order: establishment, daily sessions, authoritative requirements. All strict
mutations serialize on the establishment row, including request replay checks.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
import pytz

from sqlalchemy import select

from app.core.http.deps import has_permission
from app.core.http.errors import AppError
from app.modules.haccp import foundation_models as h
from app.modules.haccp import strict_checklist as checklist
from app.modules.haccp.foundation_contracts import HaccpBlockReason as R
from app.modules.hr.models import Establishment


def utcnow():
    return datetime.now(timezone.utc)


def fail(code, message, status=409):
    raise AppError(str(code), message, status)


async def access(session, site_id, user, permission):
    if (
        user.get("role") not in ("admin", "staff")
        or user.get("is_impersonation")
        or not has_permission(user, permission)
    ):
        fail("FORBIDDEN", "Habilitation HACCP requise.", 403)
    site = await session.scalar(
        select(Establishment).where(Establishment.id == site_id).with_for_update()
    )
    if site is None:
        fail("NOT_FOUND", "Établissement introuvable.", 404)
    if (
        user["role"] == "staff"
        and await session.get(h.HaccpStaffScope, (site_id, int(user["id"]))) is None
    ):
        fail("FORBIDDEN", "Vous n'êtes pas habilité pour cet établissement.", 403)
    return site


def local_now(site, now):
    try:
        return now.astimezone(pytz.timezone(site.timezone))
    except (pytz.UnknownTimeZoneError, ValueError):
        fail(R.NOT_CONFIGURED, "Fuseau horaire de l'établissement invalide.")


def wall_time(day, value, zone):
    try:
        return zone.localize(datetime.combine(day, value), is_dst=None)
    except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError):
        fail(
            R.NOT_CONFIGURED,
            "Créneau ambigu au changement d'heure : corrigez les horaires.",
        )


async def hours(session, site_id, day):
    return (
        await session.scalars(
            select(h.EstablishmentBusinessHours)
            .where(
                h.EstablishmentBusinessHours.establishment_id == site_id,
                h.EstablishmentBusinessHours.day_of_week == day.weekday(),
                h.EstablishmentBusinessHours.is_active,
            )
            .order_by(h.EstablishmentBusinessHours.opens_at)
        )
    ).all()


async def business_day(session, site, kind, now):
    local = local_now(site, now)
    day = local.date()
    if kind == "opening":
        tomorrow = day + timedelta(days=1)
        next_hours = await hours(session, site.id, tomorrow)
        if next_hours and next_hours[0].opens_at.hour == 0:
            first = wall_time(tomorrow, next_hours[0].opens_at, local.tzinfo)
            if first - timedelta(hours=1) <= local < first:
                day = tomorrow
        slots = await hours(session, site.id, day)
        closed = await session.scalar(
            select(h.EstablishmentClosure.id).where(
                h.EstablishmentClosure.establishment_id == site.id,
                h.EstablishmentClosure.closure_date == day,
            )
        )
        if not slots or closed is not None or not site.is_active:
            fail(R.NOT_CONFIGURED, "Aucune ouverture prévue pour cette journée.")
        first = wall_time(day, slots[0].opens_at, local.tzinfo)
        last = wall_time(day, max(slot.closes_at for slot in slots), local.tzinfo)
        if local < first - timedelta(hours=1) or local >= last:
            fail(
                "HACCP_OUTSIDE_CONTROL_WINDOW",
                "L'ouverture se contrôle à partir d'une heure avant le premier service et avant sa fermeture.",
            )
    else:
        # Finishing yesterday just after midnight is a present-time control, never backdating.
        yesterday = day - timedelta(days=1)
        slots = await hours(session, site.id, yesterday)
        if slots:
            last = wall_time(
                yesterday, max(slot.closes_at for slot in slots), local.tzinfo
            )
            opening = await daily(session, site.id, yesterday, "opening")
            closing = await daily(session, site.id, yesterday, "closing")
            if (
                opening
                and opening.status == "complete"
                and (not closing or closing.status == "in_progress")
                and local <= last + timedelta(hours=4)
            ):
                day = yesterday
    return day


async def daily(session, site_id, day, kind):
    return await session.scalar(
        select(h.HaccpDailySession)
        .where(
            h.HaccpDailySession.establishment_id == site_id,
            h.HaccpDailySession.business_date == day,
            h.HaccpDailySession.session_type == kind,
        )
        .with_for_update()
    )


async def get_check(session, site_id, session_id):
    row = await session.scalar(
        select(h.HaccpDailySession)
        .where(
            h.HaccpDailySession.id == session_id,
            h.HaccpDailySession.establishment_id == site_id,
        )
        .with_for_update()
    )
    if row is None:
        fail("NOT_FOUND", "Session introuvable dans cet établissement.", 404)
    return row


def audit(
    session,
    site_id,
    row,
    user,
    action,
    reason,
    *,
    before=None,
    after=None,
    subject=None,
):
    session.add(
        h.HaccpAuditEvent(
            establishment_id=site_id,
            session_id=row.id if row else None,
            actor_user_id=int(user["id"]),
            action=action,
            subject=subject or f"session:{row.id}",
            reason=reason,
            before=before,
            after=after,
            recorded_at=utcnow(),
        )
    )


async def requirements(session, row):
    return (
        await session.scalars(
            select(h.HaccpRequirement)
            .where(
                h.HaccpRequirement.session_id == row.id,
                h.HaccpRequirement.session_revision == row.revision,
            )
            .order_by(h.HaccpRequirement.requirement_key)
        )
    ).all()


def signatures(items):
    return {item["key"]: item["snapshot"] for item in items}


async def replace_requirements(session, row, expected):
    for item in expected:
        requirement = h.HaccpRequirement(
            session_id=row.id,
            establishment_id=row.establishment_id,
            session_revision=row.revision,
            requirement_key=item["key"],
            kind=item["kind"],
            snapshot=item["snapshot"],
            **{
                key: item[key]
                for key in ("equipment_id", "task_id", "ingredient_id")
                if key in item
            },
        )
        session.add(requirement)
        await session.flush()
        for lot in item["snapshot"].get("lots", []):
            session.add(
                h.HaccpRequirementBatch(
                    requirement_id=requirement.id,
                    batch_id=lot["id"],
                    establishment_id=row.establishment_id,
                    ingredient_id=requirement.ingredient_id,
                    batch_revision=lot["revision"],
                    effective_expires_at=datetime.fromisoformat(
                        lot["effective_expires_at"]
                    )
                    if lot["effective_expires_at"]
                    else None,
                )
            )
    await session.flush()


async def missing_closings(session, site_id, before_day):
    config = await session.get(h.EstablishmentOperations, site_id)
    if config is None or config.activated_at is None:
        return []  # No historical obligation is inferred before activation.
    opens = (
        await session.scalars(
            select(h.HaccpDailySession)
            .where(
                h.HaccpDailySession.establishment_id == site_id,
                h.HaccpDailySession.session_type == "opening",
                h.HaccpDailySession.status == "complete",
                h.HaccpDailySession.validated_at >= config.activated_at,
                h.HaccpDailySession.business_date < before_day,
            )
            .order_by(h.HaccpDailySession.business_date)
        )
    ).all()
    missing = []
    for opening in opens:
        close = await daily(session, site_id, opening.business_date, "closing")
        if close is None or close.status not in ("complete", "regularized"):
            missing.append(opening.business_date)
    return missing


async def latest_by_key(session, row):
    records = (
        await session.execute(
            select(h.HaccpRequirement.requirement_key, h.HaccpEvidence)
            .join(
                h.HaccpEvidence, h.HaccpEvidence.requirement_id == h.HaccpRequirement.id
            )
            .where(h.HaccpRequirement.session_id == row.id)
            .order_by(h.HaccpEvidence.id)
        )
    ).all()
    return {key: proof for key, proof in records}


async def evaluate(session, site, row, now, *, live=True):
    stored = await requirements(session, row)
    blockers, expected = [], []
    if live:
        expected, blockers = await checklist.build(
            session, site.id, row.business_date, row.session_type, now
        )
        if {r.requirement_key: r.snapshot for r in stored} != signatures(
            expected
        ) or row.timezone != site.timezone:
            blockers.append({"code": R.EVIDENCE_STALE, "subject": "checklist"})
    expected_by_key = {item["key"]: item for item in expected}
    if live:
        for key, proof in (await latest_by_key(session, row)).items():
            if key not in expected_by_key and not proof.is_compliant:
                blockers.append(
                    {"code": "HACCP_UNRESOLVED_NONCOMPLIANCE", "subject": key}
                )
    output = []
    for requirement in stored:
        evidence = await session.scalar(
            select(h.HaccpEvidence)
            .where(h.HaccpEvidence.requirement_id == requirement.id)
            .order_by(h.HaccpEvidence.id.desc())
            .limit(1)
        )
        problems = list(
            expected_by_key.get(requirement.requirement_key, {}).get("problems", [])
        )
        if evidence is None:
            problems.append(
                {
                    "temperature": R.TEMPERATURE_MISSING,
                    "cleaning": R.CLEANING_MISSING,
                    "dlc": R.DLC_CONFIRMATION_MISSING,
                }[requirement.kind]
            )
        elif not evidence.is_compliant:
            problems.append(
                {
                    "temperature": R.TEMPERATURE_NONCOMPLIANT,
                    "cleaning": R.CLEANING_NONCOMPLIANT,
                    "dlc": R.DLC_CONFIRMATION_MISSING,
                }[requirement.kind]
            )
        blockers.extend(
            {"code": reason, "subject": requirement.requirement_key}
            for reason in dict.fromkeys(problems)
        )
        output.append(
            {
                "id": requirement.id,
                "kind": requirement.kind,
                "snapshot": requirement.snapshot,
                "blockers": list(dict.fromkeys(problems)),
                "evidence": None
                if evidence is None
                else {
                    "id": evidence.id,
                    "is_compliant": evidence.is_compliant,
                    "payload": evidence.payload,
                    "recorded_by": evidence.recorded_by,
                    "recorded_at": evidence.recorded_at.isoformat(),
                },
            }
        )
    if live and row.session_type == "opening":
        missing = await missing_closings(session, site.id, row.business_date)
        blockers.extend(
            {"code": R.PREVIOUS_CLOSING_MISSING, "subject": day.isoformat()}
            for day in missing
        )
        closing = await daily(session, site.id, row.business_date, "closing")
        if closing and closing.status in ("complete", "regularized"):
            blockers.append({"code": R.DAY_CLOSED, "subject": "daily_closing"})
    if live and row.session_type == "closing":
        opening = await daily(session, site.id, row.business_date, "opening")
        if opening is None or opening.status != "complete":
            blockers.append({"code": R.OPENING_MISSING, "subject": "daily_opening"})
    return {
        "id": row.id,
        "establishment_id": site.id,
        "business_date": row.business_date.isoformat(),
        "session_type": row.session_type,
        "revision": row.revision,
        "status": row.status,
        "validated_by": row.validated_by,
        "validated_at": checklist.iso(row.validated_at),
        "requirements": output,
        "blockers": blockers,
        "can_validate": live and row.status == "in_progress" and not blockers,
    }


async def start(session, site, kind, user):
    now = utcnow()
    day = await business_day(session, site, kind, now)
    row = await daily(session, site.id, day, kind)
    if row is None:
        row = h.HaccpDailySession(
            establishment_id=site.id,
            business_date=day,
            timezone=site.timezone,
            session_type=kind,
        )
        session.add(row)
        await session.flush()
        expected, _ = await checklist.build(session, site.id, day, kind, now)
        await replace_requirements(session, row, expected)
        audit(
            session,
            site.id,
            row,
            user,
            "session_started",
            "Contrôle quotidien commencé",
        )
    elif row.status == "in_progress":
        expected, _ = await checklist.build(session, site.id, day, kind, now)
        stored = await requirements(session, row)
        if {r.requirement_key: r.snapshot for r in stored} != signatures(
            expected
        ) or row.timezone != site.timezone:
            before = {"revision": row.revision}
            row.revision += 1
            row.timezone = site.timezone
            await replace_requirements(session, row, expected)
            audit(
                session,
                site.id,
                row,
                user,
                "checklist_refreshed",
                "Configuration ou lots modifiés : nouvelles preuves requises",
                before=before,
                after={"revision": row.revision},
            )
    return await evaluate(session, site, row, now, live=row.status == "in_progress")


async def current_write(session, site, row, revision):
    if row.business_date != await business_day(
        session, site, row.session_type, utcnow()
    ):
        fail(
            "HACCP_CONTROL_DATE_INVALID",
            "Ce contrôle n'appartient pas à la journée courante. Régularisez la fermeture oubliée.",
        )
    if row.revision != revision:
        fail(R.EVIDENCE_STALE, "La checklist a changé. Rechargez-la.")
    if row.status != "in_progress":
        fail("HACCP_SESSION_FINALIZED", "Cette session est déjà finalisée.")
    report = await evaluate(session, site, row, utcnow())
    if any(blocker["code"] == R.EVIDENCE_STALE for blocker in report["blockers"]):
        fail(
            R.EVIDENCE_STALE,
            "Les données ont changé. Actualisez la checklist et confirmez les nouveaux contrôles.",
        )
    return report


async def replay(session, row, user, action, body):
    payload = {
        "action": action,
        "user_id": int(user["id"]),
        "body": body.model_dump(mode="json"),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    subject = "request:" + hashlib.sha256(body.idempotency_key.encode()).hexdigest()
    event = await session.scalar(
        select(h.HaccpAuditEvent).where(
            h.HaccpAuditEvent.session_id == row.id, h.HaccpAuditEvent.subject == subject
        )
    )
    if event:
        if event.after["digest"] != digest:
            fail(
                "HACCP_IDEMPOTENCY_CONFLICT",
                "Cette clé de requête a déjà été utilisée avec un autre contenu.",
            )
        if event.after["revision"] != row.revision:
            fail(R.EVIDENCE_STALE, "Cette requête appartient à une ancienne révision.")
        return event.after["result"], subject, digest
    return None, subject, digest


def receipt(session, row, user, action, subject, digest, result):
    audit(
        session,
        row.establishment_id,
        row,
        user,
        action,
        "Requête HACCP tracée",
        subject=subject,
        after={"digest": digest, "revision": row.revision, "result": result},
    )


async def record(session, site, row, body, user):
    cached, subject, digest = await replay(
        session, row, user, "evidence_recorded", body
    )
    if cached is not None:
        return cached
    report = await current_write(session, site, row, body.expected_revision)
    current = {item["id"]: item for item in report["requirements"]}
    history = await latest_by_key(session, row)
    keys = {r.id: r.requirement_key for r in await requirements(session, row)}
    for item in body.items:
        target = current.get(item.requirement_id)
        if target is None:
            fail("NOT_FOUND", "Exigence absente de cette révision.", 404)
        if target["kind"] == "temperature":
            if item.temperature is None or item.confirmed is not None:
                fail(
                    "HACCP_INVALID_EVIDENCE",
                    "Une température mesurée est requise.",
                    422,
                )
            limits = target["snapshot"]
            compliant = (
                not (limits["min"] is None and limits["max"] is None)
                and (limits["min"] is None or item.temperature >= limits["min"])
                and (limits["max"] is None or item.temperature <= limits["max"])
            )
        else:
            if item.confirmed is None or item.temperature is not None:
                fail(
                    "HACCP_INVALID_EVIDENCE",
                    "Une confirmation explicite est requise.",
                    422,
                )
            if (
                target["kind"] == "cleaning"
                and target["snapshot"]["role"] == "admin"
                and user["role"] != "admin"
            ):
                fail("FORBIDDEN", "Cette tâche nécessite un admin.", 403)
            compliant = item.confirmed
        previous_proof = history.get(keys[item.requirement_id])
        previous = (
            {"is_compliant": previous_proof.is_compliant} if previous_proof else None
        )
        if (
            previous
            and not previous["is_compliant"]
            and compliant
            and not (item.corrective_action or "").strip()
        ):
            fail(
                "HACCP_CORRECTION_REQUIRED",
                "Décrivez l'action corrective avant de corriger une non-conformité.",
                422,
            )
        if target["kind"] == "dlc":
            # Automatic stock/DLC errors cannot be overridden by a human checkbox.
            compliant = compliant and not any(
                code not in (R.DLC_CONFIRMATION_MISSING,) for code in target["blockers"]
            )
        key = hashlib.sha256(
            f"{body.idempotency_key}:{item.requirement_id}".encode()
        ).hexdigest()
        session.add(
            h.HaccpEvidence(
                establishment_id=site.id,
                session_id=row.id,
                requirement_id=item.requirement_id,
                idempotency_key=key,
                is_compliant=compliant,
                payload=item.model_dump(mode="json"),
                recorded_by=int(user["id"]),
                recorded_at=utcnow(),
            )
        )
    await session.flush()
    result = await evaluate(session, site, row, utcnow())
    receipt(session, row, user, "evidence_recorded", subject, digest, result)
    return result


async def complete(session, site, row, body, user):
    cached, subject, digest = await replay(
        session, row, user, "session_completed", body
    )
    if cached is not None:
        return cached
    report = await current_write(session, site, row, body.expected_revision)
    if report["can_validate"]:
        row.status, row.validated_by, row.validated_at = (
            "complete",
            int(user["id"]),
            utcnow(),
        )
        await session.flush()
        report = await evaluate(session, site, row, utcnow(), live=False)
    # Incomplete validation returns all blockers and does not alter the approval state.
    receipt(
        session,
        row,
        user,
        "session_completed" if row.status == "complete" else "validation_refused",
        subject,
        digest,
        report,
    )
    return report


async def prepare_regularization(session, site, user):
    days = await missing_closings(session, site.id, local_now(site, utcnow()).date())
    output = []
    for day in days:
        row = await daily(session, site.id, day, "closing")
        if row is None:
            row = h.HaccpDailySession(
                establishment_id=site.id,
                business_date=day,
                timezone=site.timezone,
                session_type="closing",
            )
            session.add(row)
            await session.flush()
            audit(
                session,
                site.id,
                row,
                user,
                "missed_closing_identified",
                "Fermeture oubliée identifiée maintenant",
            )
        output.append(
            {"id": row.id, "business_date": day.isoformat(), "revision": row.revision}
        )
    return {"missing_closings": output}


async def regularize(session, site, row, body, user):
    cached, subject, digest = await replay(
        session, row, user, "closing_regularized", body
    )
    if cached is not None:
        return cached
    if row.revision != body.expected_revision:
        fail(R.EVIDENCE_STALE, "La session a changé.")
    if (
        row.session_type != "closing"
        or row.status != "in_progress"
        or row.business_date
        not in await missing_closings(
            session, site.id, local_now(site, utcnow()).date()
        )
    ):
        fail(
            "HACCP_REGULARIZATION_INVALID",
            "Seule une fermeture réellement oubliée peut être régularisée.",
        )
    now = utcnow()
    session.add(
        h.HaccpRegularization(
            establishment_id=site.id,
            closing_session_id=row.id,
            reason=body.reason,
            actions_taken=body.actions_taken,
            recorded_by=int(user["id"]),
            recorded_at=now,
        )
    )
    row.status, row.validated_by, row.validated_at = "regularized", int(user["id"]), now
    await session.flush()
    result = {
        "id": row.id,
        "status": row.status,
        "business_date": row.business_date.isoformat(),
        "recorded_at": now.isoformat(),
        "opening_still_required": True,
    }
    receipt(session, row, user, "closing_regularized", subject, digest, result)
    audit(
        session,
        site.id,
        row,
        user,
        "regularization_explanation",
        body.reason,
        after={"actions_taken": body.actions_taken},
    )
    return result


async def reopen(session, site, row, body, user):
    cached, subject, digest = await replay(session, row, user, "day_reopened", body)
    if cached is not None:
        return cached
    if (
        row.session_type != "opening"
        or row.status != "complete"
        or row.revision != body.expected_revision
        or row.business_date != await business_day(session, site, "opening", utcnow())
    ):
        fail("HACCP_REOPEN_INVALID", "Réouverture impossible pour cette session.")
    closing = await daily(session, site.id, row.business_date, "closing")
    if closing is None or closing.status != "complete":
        fail("HACCP_REOPEN_INVALID", "Cette journée n'a pas encore été clôturée.")
    for check in (row, closing):
        before = {
            "status": check.status,
            "revision": check.revision,
            "validated_by": check.validated_by,
            "validated_at": checklist.iso(check.validated_at),
        }
        check.status, check.validated_by, check.validated_at = "in_progress", None, None
        check.revision += 1
        expected, _ = await checklist.build(
            session, site.id, check.business_date, check.session_type, utcnow()
        )
        await replace_requirements(session, check, expected)
        audit(
            session,
            site.id,
            check,
            user,
            "revalidation_required",
            body.reason,
            before=before,
            after={"revision": check.revision, "actions_taken": body.actions_taken},
        )
    result = await evaluate(session, site, row, utcnow())
    receipt(session, row, user, "day_reopened", subject, digest, result)
    return result


async def decision(session, site):
    now = utcnow()
    # Read status is always available, including closed days; never creates a session.
    day = local_now(site, now).date()
    config = await session.get(h.EstablishmentOperations, site.id)
    opening = await daily(session, site.id, day, "opening")
    closing = await daily(session, site.id, day, "closing")
    reasons = []
    if config is None or not site.is_active:
        reasons.append({"code": R.NOT_CONFIGURED, "subject": "establishment"})
    if opening is None or opening.status != "complete":
        reasons.append({"code": R.OPENING_MISSING, "subject": "daily_opening"})
    if closing and closing.status in ("complete", "regularized"):
        reasons.append({"code": R.DAY_CLOSED, "subject": "daily_closing"})
    reasons.extend(
        {"code": R.PREVIOUS_CLOSING_MISSING, "subject": d.isoformat()}
        for d in await missing_closings(session, site.id, day)
    )
    if opening and opening.status == "in_progress":
        reasons.extend((await evaluate(session, site, opening, now))["blockers"])
    return {
        "establishment_id": site.id,
        "business_date": day.isoformat(),
        "daily_haccp_satisfied": not reasons,
        "blockers": reasons,
        "strict_enabled": bool(config and config.strict_enabled),
        # Expose the effective gate state to supervision clients. The order
        # service remains authoritative; this field is informational only.
        "ordering_enforced": bool(config and config.strict_enabled),
        "activation_available": False,
    }
