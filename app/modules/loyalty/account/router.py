from fastapi import APIRouter, Depends, Query

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.modules.loyalty.account import service
from app.modules.loyalty.account.schemas import (
    CheckoutReservationCreate,
    CheckoutReservationOut,
    ExpiringPointsResponse,
    LoyaltyAccountOut,
    LoyaltyTransactionPage,
    PointsRequest,
    RedeemPointsRequest,
)

router = APIRouter()


@router.get("/me", response_model=LoyaltyAccountOut)
async def my_loyalty(current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.build_account_response(session, int(current_user["id"]))


@router.get("/transactions", response_model=LoyaltyTransactionPage)
async def my_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    type: str | None = Query(None, alias="type"),
    current_user=Depends(get_current_user),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_transactions(
            session,
            int(current_user["id"]),
            page=page,
            limit=limit,
            transaction_type=type,
        )


@router.get("/expiring", response_model=ExpiringPointsResponse)
async def my_expiring_points(current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_expiring_points_response(session, int(current_user["id"]))


@router.get("/users/{user_id}", response_model=LoyaltyAccountOut)
async def get_user_loyalty(user_id: int, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.build_account_response(session, user_id)


@router.get("/users/{user_id}/transactions", response_model=LoyaltyTransactionPage)
async def get_user_transactions(
    user_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    type: str | None = Query(None, alias="type"),
    current_user=Depends(require_role("staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_transactions(
            session,
            user_id,
            page=page,
            limit=limit,
            transaction_type=type,
        )


@router.post("/points", response_model=LoyaltyAccountOut)
async def add_points(body: PointsRequest, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        account = await service.add_points(
            session,
            body.user_id,
            body.points,
            body.reason,
            changed_by_user_id=int(current_user["id"]),
            transaction_type="manual",
            source="admin",
        )
        return LoyaltyAccountOut(id=account.id, user_id=account.user_id, points=account.points)


@router.post("/redeem", response_model=LoyaltyAccountOut)
async def redeem(body: RedeemPointsRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        account = await service.redeem_points(
            session,
            int(current_user["id"]),
            body.points,
            body.reason,
            source="checkout",
        )
        return LoyaltyAccountOut(id=account.id, user_id=account.user_id, points=account.points)


@router.post("/checkout/reservations", response_model=CheckoutReservationOut, status_code=201)
async def create_checkout_reservation(
    body: CheckoutReservationCreate,
    current_user=Depends(get_current_user),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_checkout_reservation(
            session,
            int(current_user["id"]),
            body.order_id,
            body.points_to_use,
        )


@router.post("/checkout/reservations/{reservation_id}/confirm", response_model=CheckoutReservationOut)
async def confirm_checkout_reservation(reservation_id: int, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.confirm_checkout_reservation(session, int(current_user["id"]), reservation_id)


@router.post("/checkout/reservations/{reservation_id}/cancel", response_model=CheckoutReservationOut)
async def cancel_checkout_reservation(reservation_id: int, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.cancel_checkout_reservation(session, int(current_user["id"]), reservation_id)
