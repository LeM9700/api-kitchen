"""Routes livreurs et dispatch comptoir (montees sous ``/delivery``).

Droits :
- tableau, liste des livreurs : ``orders:read`` (staff, admin) ;
- attribuer / retirer : ``orders:write`` (staff, admin) ;
- creer / modifier un livreur : admin ;
- ``/driver/*`` : role ``driver`` uniquement, et seulement ses propres livraisons.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, require_permission, require_role
from app.core.http.limiter import limiter
from app.modules.delivery import dispatch_service as svc
from app.modules.delivery.dispatch_schemas import (
    AssignRequest,
    DeliverRequest,
    DeliveryActionOut,
    DepartRequest,
    DispatchBoardOut,
    DriverCreate,
    DriverCreatedOut,
    DriverDeliveryOut,
    DriverMeOut,
    DriverOut,
    DriverRecapOut,
    DriverUpdate,
    UnassignRequest,
)

router = APIRouter()


def _action(delivery) -> DeliveryActionOut:
    return DeliveryActionOut(
        id=delivery.id, order_id=delivery.order_id, status=delivery.status, driver_id=delivery.driver_id
    )


# --------------------------------------------------------------------------- comptoir / admin


@router.get("/drivers", response_model=list[DriverOut])
async def list_drivers(
    establishment_id: int | None = Query(None, ge=1),
    current_user=Depends(require_permission("orders:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await svc.list_drivers(session, establishment_id)


@router.post("/drivers", response_model=DriverCreatedOut, status_code=201)
@limiter.limit("10/minute")
async def create_driver(
    request: Request,
    body: DriverCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await svc.create_driver(
            session,
            email=body.email,
            full_name=body.full_name,
            phone=body.phone,
            vehicle=body.vehicle,
            establishment_id=body.establishment_id,
        )


@router.patch("/drivers/{driver_id}", response_model=DriverOut)
async def update_driver(
    driver_id: int,
    body: DriverUpdate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await svc.update_driver(session, driver_id, body.model_dump(exclude_unset=True))


@router.get("/dispatch/board", response_model=DispatchBoardOut)
async def dispatch_board(
    establishment_id: int | None = Query(None, ge=1),
    current_user=Depends(require_permission("orders:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await svc.dispatch_board(session, establishment_id)


@router.post("/dispatch/assign", response_model=list[DeliveryActionOut])
@limiter.limit("60/minute")
async def assign(
    request: Request,
    body: AssignRequest,
    current_user=Depends(require_permission("orders:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        deliveries = await svc.assign(
            session,
            order_ids=body.order_ids,
            driver_id=body.driver_id,
            actor_user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
        )
        return [_action(d) for d in deliveries]


@router.post("/dispatch/unassign", response_model=DeliveryActionOut)
@limiter.limit("60/minute")
async def unassign(
    request: Request,
    body: UnassignRequest,
    current_user=Depends(require_permission("orders:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        delivery = await svc.unassign(
            session, order_id=body.order_id, actor_user_id=int(current_user["id"])
        )
        return _action(delivery)


# --------------------------------------------------------------------------- livreur


@router.get("/driver/me", response_model=DriverMeOut)
async def driver_me(current_user=Depends(require_role("driver"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        return await svc.driver_me(session, driver)


@router.get("/driver/deliveries", response_model=list[DriverDeliveryOut])
async def driver_deliveries(current_user=Depends(require_role("driver"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        return await svc.driver_deliveries(session, driver)


@router.post("/driver/depart", response_model=list[DeliveryActionOut])
@limiter.limit("30/minute")
async def driver_depart(
    request: Request,
    body: DepartRequest,
    current_user=Depends(require_role("driver")),
    arq_pool=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        deliveries = await svc.driver_depart(
            session,
            driver,
            body.delivery_ids,
            user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )
        return [_action(d) for d in deliveries]


@router.post("/driver/deliveries/{delivery_id}/arrived", response_model=DeliveryActionOut)
@limiter.limit("60/minute")
async def driver_arrived(
    request: Request, delivery_id: int, current_user=Depends(require_role("driver"))
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        delivery = await svc.driver_arrived(
            session,
            driver,
            delivery_id,
            user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
        )
        return _action(delivery)


@router.post("/driver/deliveries/{delivery_id}/delivered", response_model=DeliveryActionOut)
@limiter.limit("60/minute")
async def driver_delivered(
    request: Request,
    delivery_id: int,
    body: DeliverRequest | None = None,
    current_user=Depends(require_role("driver")),
    arq_pool=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        delivery = await svc.driver_deliver(
            session,
            driver,
            delivery_id,
            cash_received=body.cash_received if body else None,
            user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )
        return _action(delivery)


@router.get("/driver/recap", response_model=DriverRecapOut)
async def driver_recap(
    day: date | None = Query(None), current_user=Depends(require_role("driver"))
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        return await svc.driver_recap(session, driver, day)
