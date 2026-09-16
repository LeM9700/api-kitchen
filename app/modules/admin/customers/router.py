from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, get_client_ip, get_pagination, require_role
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.admin.customers import service
from app.modules.admin.customers.schemas import (
    AdminCustomerDetail,
    AdminCustomerListItem,
    AdminCustomerOrderDetail,
    BulkCustomerMessageRequest,
    CustomerCommunicationOut,
    CustomerMessageSendRequest,
    CustomerMessageSendResult,
    CustomerPrivacyActionRequest,
    CustomerPrivacyExport,
    MessageTemplateOut,
)

router = APIRouter()


def _user_agent(request: Request) -> str | None:
    return request.headers.get("User-Agent")


@router.get("", response_model=PaginatedResponse[AdminCustomerListItem])
async def list_customers(
    request: Request,
    pagination: PaginationParams = Depends(get_pagination),
    query: str | None = Query(None, max_length=255),
    is_active: bool | None = None,
    email_verified: bool | None = None,
    marketing_email_opt_in: bool | None = None,
    marketing_push_opt_in: bool | None = None,
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_customers(
            session,
            pagination,
            query=query,
            is_active=is_active,
            email_verified=email_verified,
            marketing_email_opt_in=marketing_email_opt_in,
            marketing_push_opt_in=marketing_push_opt_in,
        )
        await service.record_admin_audit(
            session,
            actor=current_user,
            action="customers_list_viewed",
            target_type="customer",
            target_id="list",
            metadata={"query": query},
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )
        await session.commit()
    return PaginatedResponse.build(items, total, pagination)


@router.get("/export/csv", response_class=PlainTextResponse)
async def export_customers_csv(
    request: Request,
    query: str | None = Query(None, max_length=255),
    is_active: bool | None = None,
    email_verified: bool | None = None,
    marketing_email_opt_in: bool | None = None,
    marketing_push_opt_in: bool | None = None,
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        csv_text = await service.export_customers_csv(
            session,
            query=query,
            is_active=is_active,
            email_verified=email_verified,
            marketing_email_opt_in=marketing_email_opt_in,
            marketing_push_opt_in=marketing_push_opt_in,
        )
        await service.record_admin_audit(
            session,
            actor=current_user,
            action="customers_exported",
            target_type="customer",
            target_id="csv",
            metadata={"query": query},
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )
        await session.commit()
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get("/message-templates", response_model=list[MessageTemplateOut])
async def message_templates(current_user: dict = Depends(require_role("admin"))):
    return service.list_templates()


@router.post("/messages/bulk", response_model=CustomerMessageSendResult)
async def send_bulk_message(
    request: Request,
    body: BulkCustomerMessageRequest,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.send_bulk_customer_message(
            session,
            tenant_slug=current_user["tenant_slug"],
            body=body,
            actor=current_user,
            arq_pool=arq_pool,
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )


@router.get("/{customer_id}", response_model=AdminCustomerDetail)
async def get_customer_detail(
    request: Request,
    customer_id: int,
    pagination: PaginationParams = Depends(get_pagination),
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        detail = await service.get_customer_detail(session, customer_id, pagination)
        await service.record_admin_audit(
            session,
            actor=current_user,
            action="customer_profile_viewed",
            target_type="customer",
            target_id=customer_id,
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )
        await session.commit()
        return detail


@router.get("/{customer_id}/orders/{order_id}", response_model=AdminCustomerOrderDetail)
async def get_customer_order_detail(
    customer_id: int,
    order_id: int,
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_customer_order_detail(session, customer_id, order_id)


@router.get("/{customer_id}/communications", response_model=PaginatedResponse[CustomerCommunicationOut])
async def list_customer_communications(
    customer_id: int,
    pagination: PaginationParams = Depends(get_pagination),
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_customer_communications(session, customer_id, pagination)
    return PaginatedResponse.build(items, total, pagination)


@router.post("/{customer_id}/communications", response_model=CustomerMessageSendResult)
async def send_customer_message(
    request: Request,
    customer_id: int,
    body: CustomerMessageSendRequest,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.send_customer_message(
            session,
            tenant_slug=current_user["tenant_slug"],
            customer_id=customer_id,
            body=body,
            actor=current_user,
            arq_pool=arq_pool,
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )


@router.get("/{customer_id}/privacy-export", response_model=CustomerPrivacyExport)
async def privacy_export(
    request: Request,
    customer_id: int,
    current_user: dict = Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        payload = await service.privacy_export(session, customer_id)
        await service.record_admin_audit(
            session,
            actor=current_user,
            action="customer_privacy_exported",
            target_type="customer",
            target_id=customer_id,
            metadata={"exported_at": datetime.utcnow().isoformat()},
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )
        await session.commit()
        return payload


@router.post("/{customer_id}/privacy-delete", status_code=204)
async def privacy_delete(
    customer_id: int,
    body: CustomerPrivacyActionRequest,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> None:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.deactivate_customer(
            session,
            customer_id=customer_id,
            reason=body.reason,
            actor=current_user,
            redis=arq_pool,
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )


@router.post("/{customer_id}/anonymize", status_code=204)
async def anonymize_customer(
    customer_id: int,
    body: CustomerPrivacyActionRequest,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> None:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.anonymize_customer(
            session,
            customer_id=customer_id,
            reason=body.reason,
            actor=current_user,
            redis=arq_pool,
            ip_address=get_client_ip(request),
            user_agent=_user_agent(request),
        )
