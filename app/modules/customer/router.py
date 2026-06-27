from fastapi import APIRouter, Depends, Header, Request

from app.core.http.deps import get_current_user, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.auth.schemas import TokenResponse
from app.modules.customer import service
from app.modules.customer.schemas import (
    CustomerDeleteRequest,
    CustomerOut,
    CustomerRegisterRequest,
    CustomerUpdateRequest,
)

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: CustomerRegisterRequest,
    x_tenant_slug: str | None = Header(default=None, alias="x-tenant-slug"),
):
    if x_tenant_slug is None:
        raise AppError("MISSING_TENANT_SLUG", "X-Tenant-Slug header is required", 400)
    arq_pool = getattr(request.app.state, "arq_pool", None)
    _, access, refresh, session_id = await service.register(x_tenant_slug, body, arq_pool=arq_pool)
    return TokenResponse(access_token=access, refresh_token=refresh, session_id=session_id)


@router.get("/me", response_model=CustomerOut)
async def get_me(current_user: dict = Depends(require_role("customer"))):
    return await service.get_profile(int(current_user["id"]), current_user["tenant_slug"])


@router.patch("/me", response_model=CustomerOut)
async def update_me(
    body: CustomerUpdateRequest,
    current_user: dict = Depends(require_role("customer")),
):
    return await service.update_profile(int(current_user["id"]), current_user["tenant_slug"], body)


@router.delete("/me", status_code=204)
async def delete_me(
    request: Request,
    body: CustomerDeleteRequest,
    current_user: dict = Depends(require_role("customer")),
):
    redis = getattr(request.app.state, "arq_pool", None)
    await service.delete_account(
        int(current_user["id"]),
        current_user["tenant_slug"],
        body.password,
        redis=redis,
    )
