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
from app.modules.delivery import dispatch_settings
from app.modules.delivery import failures as failures_svc
from app.modules.delivery import tracking
from app.modules.delivery.dispatch_schemas import (
    AvailableOrdersOut,
    ClaimRequest,
    EstablishmentDispatchSettingsOut,
    EstablishmentDispatchSettingsUpdate,
    AssignRequest,
    DeliverRequest,
    DeliverWithoutCodeRequest,
    DeliveryActionOut,
    DepartRequest,
    DispatchBoardOut,
    DriverCreate,
    DriverCreatedOut,
    DriverDeliveryOut,
    DriverFailureRequest,
    FailureOut,
    DriverMeOut,
    DriverOut,
    DriverRecapOut,
    DriverUpdate,
    LiveDriverOut,
    LocationAckOut,
    LocationBatchIn,
    LocationConsentIn,
    ResolveFailureRequest,
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
            code=body.code if body else None,
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


@router.post("/driver/deliveries/{delivery_id}/failed", response_model=FailureOut, status_code=201)
@limiter.limit("30/minute")
async def driver_failed(
    request: Request,
    delivery_id: int,
    body: DriverFailureRequest,
    current_user=Depends(require_role("driver")),
    arq_pool=Depends(get_arq_pool),
):
    """Le livreur declare l'echec de la livraison (motif, appels passes). Un administrateur statue ensuite."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        failure = await failures_svc.report_failure(
            session,
            driver,
            delivery_id,
            reason=body.reason,
            note=body.note,
            call_attempts=body.call_attempts,
            user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )
        (item,) = [
            f
            for f in await failures_svc.list_failures(session, None)
            if f["id"] == failure.id
        ]
        return item


# --------------------------------------------------------------------------- echecs a traiter


@router.get("/failures", response_model=list[FailureOut])
async def list_failures(
    status: str | None = Query("pending", pattern="^(pending|resolved)$"),
    current_user=Depends(require_permission("orders:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await failures_svc.list_failures(session, status)


@router.post("/failures/{failure_id}/resolve", response_model=FailureOut)
@limiter.limit("30/minute")
async def resolve_failure(
    request: Request,
    failure_id: int,
    body: ResolveFailureRequest,
    current_user=Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
):
    """Rembourser, retenir des frais (faute du client uniquement) ou relivrer. Administrateur uniquement."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await failures_svc.resolve_failure(
            session,
            failure_id,
            action=body.action,
            amount_cents=body.amount,
            note=body.note,
            admin_user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )
        (item,) = [f for f in await failures_svc.list_failures(session, None) if f["id"] == failure_id]
        return item


@router.post("/dispatch/orders/{order_id}/deliver-without-code", response_model=DeliveryActionOut)
@limiter.limit("20/minute")
async def deliver_without_code(
    request: Request,
    order_id: int,
    body: DeliverWithoutCodeRequest,
    current_user=Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
):
    """Conclut une livraison sans le code du client. Administrateur uniquement, motif obligatoire, journalise."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        delivery = await svc.admin_deliver_without_code(
            session,
            order_id=order_id,
            reason=body.reason,
            admin_user_id=int(current_user["id"]),
            tenant_slug=current_user["tenant_slug"],
            arq_pool=arq_pool,
        )
        return _action(delivery)


# --------------------------------------------------------------------------- GPS


@router.post("/driver/location-consent", status_code=204)
async def grant_location_consent(body: LocationConsentIn, current_user=Depends(require_role("driver"))):
    """Le livreur accepte le partage de sa position pendant ses livraisons (version du texte incluse)."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        await tracking.grant_consent(session, driver, body.version)


@router.delete("/driver/location-consent", status_code=204)
async def withdraw_location_consent(current_user=Depends(require_role("driver"))):
    """Retire l'accord : plus aucune position n'est acceptee."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        await tracking.withdraw_consent(session, driver)


@router.post("/driver/location", response_model=LocationAckOut)
@limiter.limit("60/minute")
async def driver_location(
    request: Request, body: LocationBatchIn, current_user=Depends(require_role("driver"))
):
    """Positions du livreur (un point, ou un lot accumule hors reseau). Refusees hors livraison active."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        return await tracking.ingest(session, driver, [p.model_dump() for p in body.points])


@router.get("/live", response_model=list[LiveDriverOut])
async def live_drivers(
    establishment_id: int | None = Query(None, ge=1),
    current_user=Depends(require_permission("orders:read", "staff", "admin")),
):
    """Carte du comptoir : livreurs actifs, position (seulement en livraison), signal, charge."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tracking.live_board(session, establishment_id)


# --------------------------------------------------------------------------- auto-attribution


@router.get("/driver/available", response_model=AvailableOrdersOut)
async def driver_available(current_user=Depends(require_role("driver"))):
    """Commandes que le livreur peut prendre lui-meme (mode self_assign de son etablissement)."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        return await svc.claimable_orders(session, driver)


@router.post("/driver/claim", response_model=list[DeliveryActionOut])
@limiter.limit("30/minute")
async def driver_claim(
    request: Request, body: ClaimRequest, current_user=Depends(require_role("driver"))
):
    """Prend une ou plusieurs commandes (tout ou rien). Jamais une commande deja prise."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        deliveries = await svc.claim(session, driver, body.order_ids, user_id=int(current_user["id"]))
        return [_action(d) for d in deliveries]


@router.post("/driver/deliveries/{delivery_id}/release", response_model=DeliveryActionOut)
@limiter.limit("30/minute")
async def driver_release(
    request: Request, delivery_id: int, current_user=Depends(require_role("driver"))
):
    """Repose une commande prise et pas encore partie (mode self_assign)."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        driver = await svc.get_driver_for_user(session, int(current_user["id"]))
        delivery = await svc.release(session, driver, delivery_id, user_id=int(current_user["id"]))
        return _action(delivery)


@router.get(
    "/establishments/{establishment_id}/dispatch-settings",
    response_model=EstablishmentDispatchSettingsOut,
)
async def get_dispatch_settings(
    establishment_id: int,
    current_user=Depends(require_permission("orders:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await dispatch_settings.get_effective(session, establishment_id)


@router.put(
    "/establishments/{establishment_id}/dispatch-settings",
    response_model=EstablishmentDispatchSettingsOut,
)
@limiter.limit("20/minute")
async def put_dispatch_settings(
    request: Request,
    establishment_id: int,
    body: EstablishmentDispatchSettingsUpdate,
    current_user=Depends(require_role("admin")),
):
    """Mode d'attribution, plafond et regles d'echec d'un etablissement (administrateur, audite)."""
    values = body.model_dump(exclude_unset=True)
    expected_version = values.pop("expected_version")
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await dispatch_settings.update(
            session,
            establishment_id,
            values,
            expected_version=expected_version,
            user_id=int(current_user["id"]),
            user_email=current_user.get("email"),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
