from fastapi import APIRouter, Depends, Header, Query, Request, status
from slowapi.util import get_remote_address

from app.core.database import get_tenant_session
from app.core.http.deps import require_permission, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.kds import service
from app.modules.kds.schemas import (
    KdsPairingCodeOut,
    KdsPairRequest,
    KdsRemoteSessionOut,
    KdsRemoteSessionRevokeOut,
    KdsRemoteSessionStatusOut,
    KdsScreenCreate,
    KdsScreenOut,
    KdsScreenSessionsRevokedOut,
    KdsScreenUpdate,
)

router = APIRouter()


def _kds_pair_rate_limit_key(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    tenant_slug = getattr(request.state, "tenant_slug", None)
    if user_id:
        return f"user:{tenant_slug}:{user_id}"
    return f"ip:{get_remote_address(request)}"


async def get_kds_remote_session(
    x_kds_session: str | None = Header(None, alias="X-KDS-Session"),
    current_user: dict = Depends(require_permission("orders:preparation", "staff", "admin")),
) -> dict:
    if not x_kds_session:
        raise AppError("KDS_SESSION_INVALID", "KDS session token required", 401)
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.resolve_remote_session(
            session,
            tenant_slug=current_user["tenant_slug"],
            token=x_kds_session,
        )


@router.get("/screens", response_model=list[KdsScreenOut])
async def list_screens(
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_permission("orders:preparation", "staff", "admin")),
) -> list[KdsScreenOut]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_screens(session, include_inactive=include_inactive)


@router.post("/screens", response_model=KdsScreenOut, status_code=status.HTTP_201_CREATED)
async def create_screen(
    body: KdsScreenCreate,
    current_user: dict = Depends(require_role("admin")),
) -> KdsScreenOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_screen(session, body)


@router.patch("/screens/{screen_id}", response_model=KdsScreenOut)
async def update_screen(
    screen_id: int,
    body: KdsScreenUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> KdsScreenOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_screen(session, screen_id, body)


@router.post("/screens/{screen_id}/pairing-code", response_model=KdsPairingCodeOut)
async def create_pairing_code(
    screen_id: int,
    current_user: dict = Depends(require_permission("orders:preparation", "staff", "admin")),
) -> KdsPairingCodeOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_pairing_code(
            session,
            tenant_slug=current_user["tenant_slug"],
            screen_id=screen_id,
            created_by_user_id=int(current_user["id"]),
        )


@router.post("/pair", response_model=KdsRemoteSessionOut)
@limiter.limit("5/minute", key_func=_kds_pair_rate_limit_key)
async def pair_remote(
    request: Request,
    body: KdsPairRequest,
    current_user: dict = Depends(require_permission("orders:preparation", "staff", "admin")),
) -> KdsRemoteSessionOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.pair_remote(
            session,
            tenant_slug=current_user["tenant_slug"],
            code=body.code,
            paired_by_user_id=int(current_user["id"]),
            device_label=body.device_label,
        )


@router.get("/remote/session", response_model=KdsRemoteSessionStatusOut)
async def get_remote_session(
    remote_session: dict = Depends(get_kds_remote_session),
) -> KdsRemoteSessionStatusOut:
    return remote_session


@router.post("/remote/session/revoke", response_model=KdsRemoteSessionRevokeOut)
async def revoke_current_remote_session(
    x_kds_session: str | None = Header(None, alias="X-KDS-Session"),
    current_user: dict = Depends(require_permission("orders:preparation", "staff", "admin")),
) -> KdsRemoteSessionRevokeOut:
    if not x_kds_session:
        raise AppError("KDS_SESSION_INVALID", "KDS session token required", 401)
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.revoke_remote_session(
            session,
            tenant_slug=current_user["tenant_slug"],
            token=x_kds_session,
        )


@router.post("/screens/{screen_id}/revoke-sessions", response_model=KdsScreenSessionsRevokedOut)
async def revoke_screen_sessions(
    screen_id: int,
    current_user: dict = Depends(require_role("admin")),
) -> KdsScreenSessionsRevokedOut:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.revoke_screen_sessions(session, screen_id=screen_id)
