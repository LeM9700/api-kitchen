"""Staff-only strict workflow. No order/payment integration or activation route."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends
from sqlalchemy.exc import DBAPIError

from app.core.database import get_tenant_session
from app.core.http.deps import require_permission
from app.core.http.errors import AppError
from app.modules.haccp import strict_service as service
from app.modules.haccp import foundation_models as h
from app.modules.auth.models import User
from app.modules.haccp.strict_contracts import (
    ExplainRequest,
    PositiveId,
    RecordEvidence,
    RevisionRequest,
    StartSession,
)

router = APIRouter(prefix="/strict/establishments/{establishment_id}")


async def assign_staff(session, site, user_id, actor, enabled):
    if actor["role"] != "admin":
        raise AppError(
            "FORBIDDEN", "Seul un admin peut gérer les affectations HACCP.", 403
        )
    target = await session.get(User, user_id)
    if target is None or target.role != "staff" or (enabled and not target.is_active):
        raise AppError("NOT_FOUND", "Compte staff actif introuvable.", 404)
    assignment = await session.get(h.HaccpStaffScope, (site.id, user_id))
    if enabled and assignment is None:
        session.add(
            h.HaccpStaffScope(
                establishment_id=site.id, user_id=user_id, assigned_by=int(actor["id"])
            )
        )
    elif not enabled and assignment is not None:
        await session.delete(assignment)
    service.audit(
        session,
        site.id,
        None,
        actor,
        "staff_scope_changed",
        "Affectation HACCP modifiée par un admin",
        subject=f"staff:{user_id}",
        before={"assigned": assignment is not None},
        after={"assigned": enabled},
    )
    return {"user_id": user_id, "establishment_id": site.id, "assigned": enabled}


@router.put("/staff/{user_id}")
async def grant_scope(
    establishment_id: PositiveId,
    user_id: PositiveId,
    user: dict = Depends(require_permission("haccp:manage")),
):
    async with transaction(user, establishment_id, "haccp:manage") as (session, site):
        return await assign_staff(session, site, user_id, user, True)


@router.delete("/staff/{user_id}")
async def revoke_scope(
    establishment_id: PositiveId,
    user_id: PositiveId,
    user: dict = Depends(require_permission("haccp:manage")),
):
    async with transaction(user, establishment_id, "haccp:manage") as (session, site):
        return await assign_staff(session, site, user_id, user, False)


@asynccontextmanager
async def transaction(user, site_id, permission):
    try:
        async with get_tenant_session(user["tenant_slug"]) as session:
            # `get_tenant_session` may already have an open transaction (for
            # example in the ASGI test harness). Row locks in `access()` and
            # the database uniqueness constraints provide the same retryable
            # protection without attempting to change isolation mid-transaction.
            site = await service.access(session, site_id, user, permission)
            yield session, site
            await session.commit()
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("40001", "40P01", "23505"):
            raise AppError(
                "HACCP_RETRY_REQUIRED",
                "Les données ont changé simultanément. Réessayez avec la même clé.",
                409,
            ) from error
        raise


@router.get("/status")
async def status(
    establishment_id: PositiveId, user: dict = Depends(require_permission("haccp:read"))
):
    async with transaction(user, establishment_id, "haccp:read") as (session, site):
        return await service.decision(session, site)


@router.post("/sessions")
async def start(
    establishment_id: PositiveId,
    body: StartSession,
    user: dict = Depends(require_permission("haccp:write")),
):
    async with transaction(user, establishment_id, "haccp:write") as (session, site):
        return await service.start(session, site, body.session_type, user)


@router.get("/sessions/{session_id}")
async def inspect(
    establishment_id: PositiveId,
    session_id: PositiveId,
    user: dict = Depends(require_permission("haccp:read")),
):
    async with transaction(user, establishment_id, "haccp:read") as (session, site):
        row = await service.get_check(session, site.id, session_id)
        return await service.evaluate(
            session,
            site,
            row,
            service.utcnow(),
            live=row.status == "in_progress"
            and row.business_date == service.local_now(site, service.utcnow()).date(),
        )


@router.post("/sessions/{session_id}/evidence")
async def evidence(
    establishment_id: PositiveId,
    session_id: PositiveId,
    body: RecordEvidence,
    user: dict = Depends(require_permission("haccp:write")),
):
    async with transaction(user, establishment_id, "haccp:write") as (session, site):
        row = await service.get_check(session, site.id, session_id)
        return await service.record(session, site, row, body, user)


@router.post("/sessions/{session_id}/complete")
async def complete(
    establishment_id: PositiveId,
    session_id: PositiveId,
    body: RevisionRequest,
    user: dict = Depends(require_permission("haccp:validate")),
):
    async with transaction(user, establishment_id, "haccp:validate") as (session, site):
        row = await service.get_check(session, site.id, session_id)
        return await service.complete(session, site, row, body, user)


@router.post("/regularizations/prepare")
async def prepare(
    establishment_id: PositiveId,
    user: dict = Depends(require_permission("haccp:validate")),
):
    async with transaction(user, establishment_id, "haccp:validate") as (session, site):
        return await service.prepare_regularization(session, site, user)


@router.post("/sessions/{session_id}/regularize")
async def regularize(
    establishment_id: PositiveId,
    session_id: PositiveId,
    body: ExplainRequest,
    user: dict = Depends(require_permission("haccp:validate")),
):
    async with transaction(user, establishment_id, "haccp:validate") as (session, site):
        row = await service.get_check(session, site.id, session_id)
        return await service.regularize(session, site, row, body, user)


@router.post("/sessions/{session_id}/reopen")
async def reopen(
    establishment_id: PositiveId,
    session_id: PositiveId,
    body: ExplainRequest,
    user: dict = Depends(require_permission("haccp:validate")),
):
    async with transaction(user, establishment_id, "haccp:validate") as (session, site):
        row = await service.get_check(session, site.id, session_id)
        return await service.reopen(session, site, row, body, user)
