from arq import ArqRedis
from fastapi import APIRouter, Depends, Query

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, get_current_user, require_role
from app.modules.loyalty.account import service
from app.modules.loyalty.account.schemas import (
    CheckoutReservationCreate,
    CheckoutReservationOut,
    ExpiringPointsResponse,
    LoyaltyAccountOut,
    LoyaltyQrIdentifyRequest,
    LoyaltyQrTokenResponse,
    LoyaltyStaffCustomerCreateRequest,
    LoyaltyStaffCustomerSearchResponse,
    LoyaltyStaffCustomerWalletOut,
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


@router.post("/qr-token", response_model=LoyaltyQrTokenResponse)
async def create_my_loyalty_qr_token(current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_loyalty_qr_token(
            session,
            user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
        )


@router.get("/staff/customers/search", response_model=LoyaltyStaffCustomerSearchResponse)
async def staff_search_customers(
    q: str = Query(..., min_length=1, max_length=32),
    limit: int = Query(10, ge=1, le=20),
    current_user=Depends(require_role("staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.search_staff_customers(session, q, actor=current_user, limit=limit)


@router.post("/staff/customers", response_model=LoyaltyStaffCustomerWalletOut, status_code=201)
async def staff_create_customer(
    body: LoyaltyStaffCustomerCreateRequest,
    current_user=Depends(require_role("staff", "admin")),
    arq_pool: ArqRedis = Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_staff_customer(
            session,
            body,
            actor=current_user,
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )


@router.get("/staff/customers/{customer_id}/wallet", response_model=LoyaltyStaffCustomerWalletOut)
async def staff_customer_wallet(customer_id: int, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.build_staff_customer_wallet(session, customer_id, actor=current_user)


@router.post("/staff/identify-qr", response_model=LoyaltyStaffCustomerWalletOut)
async def staff_identify_qr(
    body: LoyaltyQrIdentifyRequest,
    current_user=Depends(require_role("staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.identify_loyalty_qr_token(
            session,
            body.token,
            tenant_slug=current_user["tenant_slug"],
            actor=current_user,
        )


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
