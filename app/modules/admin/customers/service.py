import csv
from datetime import datetime, timezone
from io import StringIO
from typing import Any

from arq import ArqRedis
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.token_revocation import flag_user_disabled, publish_session_revoked
from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.admin.customers.models import AdminAuditLog, CustomerCommunication
from app.modules.admin.customers.schemas import (
    AdminCustomerListItem,
    BulkCustomerMessageRequest,
    CustomerMessageSendRequest,
    MessageTemplateOut,
)
from app.modules.auth.models import RefreshToken, User
from app.modules.loyalty.account.models import LoyaltyAccount
from app.modules.notifications.notification_service import notify_user
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order
from app.modules.payments import service as payments_service


MESSAGE_TEMPLATES: dict[str, MessageTemplateOut] = {
    "promo": MessageTemplateOut(
        key="promo",
        label="Promotion",
        message_type="marketing",
        channels=["email", "push"],
        subject="Une offre vous attend",
        body="Une nouvelle offre est disponible dans votre application.",
    ),
    "order_delay": MessageTemplateOut(
        key="order_delay",
        label="Retard commande",
        message_type="transactional",
        channels=["email", "push"],
        subject="Votre commande prend un peu de retard",
        body="Votre commande prend un peu de retard. Merci pour votre patience.",
    ),
    "loyalty": MessageTemplateOut(
        key="loyalty",
        label="Fidelite",
        message_type="marketing",
        channels=["email", "push"],
        subject="Vos avantages fidelite",
        body="Consultez vos avantages fidelite disponibles dans l'application.",
    ),
}


async def record_admin_audit(
    session: AsyncSession,
    *,
    actor: dict | None,
    action: str,
    target_type: str,
    target_id: int | str,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            actor_user_id=int(actor["id"]) if actor and actor.get("id") is not None else None,
            actor_email=actor.get("email") if actor else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            metadata_json=metadata or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )


def list_templates() -> list[MessageTemplateOut]:
    return list(MESSAGE_TEMPLATES.values())


def _customer_filters(
    *,
    query: str | None = None,
    is_active: bool | None = None,
    email_verified: bool | None = None,
    marketing_email_opt_in: bool | None = None,
    marketing_push_opt_in: bool | None = None,
) -> list:
    filters = [User.role == "customer"]
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(or_(User.email.ilike(pattern), User.full_name.ilike(pattern), User.phone.ilike(pattern)))
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))
    if email_verified is not None:
        filters.append(User.email_verified_at.isnot(None) if email_verified else User.email_verified_at.is_(None))
    if marketing_email_opt_in is not None:
        filters.append(User.marketing_email_opt_in.is_(marketing_email_opt_in))
    if marketing_push_opt_in is not None:
        filters.append(User.marketing_push_opt_in.is_(marketing_push_opt_in))
    return filters


async def _customer_summary(session: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not user_ids:
        return {}

    order_rows = await session.execute(
        select(
            Order.user_id,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total), 0),
            func.max(Order.created_at),
        )
        .where(Order.user_id.in_(user_ids))
        .group_by(Order.user_id)
    )
    summaries = {
        int(row[0]): {
            "order_count": int(row[1] or 0),
            "total_spent": round(float(row[2] or 0), 2),
            "last_order_at": row[3],
            "loyalty_points": 0,
        }
        for row in order_rows
    }

    loyalty_rows = await session.execute(
        select(LoyaltyAccount.user_id, LoyaltyAccount.points).where(LoyaltyAccount.user_id.in_(user_ids))
    )
    for user_id, points in loyalty_rows:
        summaries.setdefault(int(user_id), {
            "order_count": 0,
            "total_spent": 0.0,
            "last_order_at": None,
            "loyalty_points": 0,
        })["loyalty_points"] = int(points or 0)

    return summaries


def _build_customer_item(user: User, summary: dict[str, Any] | None = None) -> AdminCustomerListItem:
    summary = summary or {}
    return AdminCustomerListItem(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        is_active=user.is_active,
        email_verified=user.email_verified_at is not None,
        marketing_email_opt_in=bool(getattr(user, "marketing_email_opt_in", False)),
        marketing_push_opt_in=bool(getattr(user, "marketing_push_opt_in", False)),
        order_count=int(summary.get("order_count", 0)),
        total_spent=float(summary.get("total_spent", 0)),
        last_order_at=summary.get("last_order_at"),
        loyalty_points=int(summary.get("loyalty_points", 0)),
        created_at=user.created_at,
    )


async def list_customers(
    session: AsyncSession,
    pagination: PaginationParams,
    *,
    query: str | None = None,
    is_active: bool | None = None,
    email_verified: bool | None = None,
    marketing_email_opt_in: bool | None = None,
    marketing_push_opt_in: bool | None = None,
) -> tuple[list[AdminCustomerListItem], int]:
    filters = _customer_filters(
        query=query,
        is_active=is_active,
        email_verified=email_verified,
        marketing_email_opt_in=marketing_email_opt_in,
        marketing_push_opt_in=marketing_push_opt_in,
    )
    total = int(await session.scalar(select(func.count()).select_from(User).where(*filters)) or 0)
    result = await session.execute(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    users = list(result.scalars())
    summaries = await _customer_summary(session, [user.id for user in users])
    return [_build_customer_item(user, summaries.get(user.id)) for user in users], total


async def export_customers_csv(session: AsyncSession, **filters) -> str:
    result = await session.execute(
        select(User)
        .where(*_customer_filters(**filters))
        .order_by(User.created_at.desc(), User.id.desc())
    )
    users = list(result.scalars())
    summaries = await _customer_summary(session, [user.id for user in users])

    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "email",
            "full_name",
            "phone",
            "is_active",
            "email_verified",
            "marketing_email_opt_in",
            "marketing_push_opt_in",
            "order_count",
            "total_spent",
            "last_order_at",
            "loyalty_points",
            "created_at",
        ],
    )
    writer.writeheader()
    for user in users:
        item = _build_customer_item(user, summaries.get(user.id)).model_dump()
        writer.writerow(item)
    return output.getvalue()


async def get_customer(session: AsyncSession, customer_id: int) -> User:
    user = await session.get(User, customer_id)
    if user is None or user.role != "customer":
        raise AppError("CUSTOMER_NOT_FOUND", "Customer not found", 404)
    return user


async def get_customer_detail(session: AsyncSession, customer_id: int, pagination: PaginationParams) -> dict:
    user = await get_customer(session, customer_id)
    summaries = await _customer_summary(session, [customer_id])
    orders, _ = await orders_service.list_my_orders(session, pagination, customer_id)
    return {
        **_build_customer_item(user, summaries.get(customer_id)).model_dump(),
        "orders": orders,
    }


async def get_customer_order_detail(session: AsyncSession, customer_id: int, order_id: int) -> dict:
    await get_customer(session, customer_id)
    order = await session.get(Order, order_id)
    if order is None or order.user_id != customer_id:
        raise AppError("ORDER_NOT_FOUND", "Order not found", 404)
    detail = await orders_service.get_order_detail(session, order_id, user_id=customer_id, is_staff=True)
    try:
        payment = await payments_service.get_payment_for_order(session, order_id, user_id=customer_id, is_staff=True)
    except AppError as exc:
        if exc.code != "PAYMENT_NOT_FOUND":
            raise
        payment = None
    return {"order": detail, "payment": payment}


async def list_customer_communications(
    session: AsyncSession,
    customer_id: int,
    pagination: PaginationParams,
) -> tuple[list[CustomerCommunication], int]:
    await get_customer(session, customer_id)
    filters = [CustomerCommunication.user_id == customer_id]
    total = int(await session.scalar(select(func.count()).select_from(CustomerCommunication).where(*filters)) or 0)
    result = await session.execute(
        select(CustomerCommunication)
        .where(*filters)
        .order_by(CustomerCommunication.created_at.desc(), CustomerCommunication.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


def _resolve_message(body: CustomerMessageSendRequest) -> tuple[str | None, str, str | None, str]:
    template = MESSAGE_TEMPLATES.get(body.template_key or "") if body.template_key else None
    if body.template_key and template is None:
        raise AppError("MESSAGE_TEMPLATE_NOT_FOUND", "Message template not found", 404, "template_key")
    message_type = body.message_type
    if template is not None:
        message_type = template.message_type
    subject = body.subject or (template.subject if template else None)
    text = body.body or (template.body if template else None)
    if not text:
        raise AppError("MESSAGE_BODY_REQUIRED", "Message body is required", 422, "body")
    return subject, text, body.template_key, message_type


def _can_send(user: User, channel: str, message_type: str) -> bool:
    if message_type == "transactional":
        return True
    if channel == "email":
        return bool(user.marketing_email_opt_in)
    if channel == "push":
        return bool(user.marketing_push_opt_in)
    return False


async def _send_to_user(
    session: AsyncSession,
    *,
    tenant_slug: str,
    user: User,
    body: CustomerMessageSendRequest,
    actor_user_id: int,
    arq_pool: ArqRedis | None,
) -> tuple[list[int], int, list[dict[str, Any]]]:
    subject, text, template_key, message_type = _resolve_message(body)
    ids: list[int] = []
    actions: list[dict[str, Any]] = []
    skipped = 0

    for channel in body.channels:
        if not _can_send(user, channel, message_type):
            session.add(
                CustomerCommunication(
                    user_id=user.id,
                    channel=channel,
                    message_type=message_type,
                    template_key=template_key,
                    subject=subject,
                    body=text,
                    status="skipped",
                    error="marketing_opt_out",
                    sent_by_user_id=actor_user_id,
                )
            )
            skipped += 1
            continue

        communication = CustomerCommunication(
            user_id=user.id,
            channel=channel,
            message_type=message_type,
            template_key=template_key,
            subject=subject,
            body=text,
            status="queued" if channel == "email" else "sent",
            sent_by_user_id=actor_user_id,
            sent_at=datetime.now(timezone.utc) if channel == "push" else None,
        )
        session.add(communication)
        await session.flush()
        ids.append(communication.id)

        if channel == "email" and arq_pool is not None:
            actions.append({"type": "email", "communication_id": communication.id})
        elif channel == "email":
            communication.status = "skipped"
            communication.error = "email_worker_unavailable"
            skipped += 1
        else:
            actions.append(
                {
                    "type": "push",
                    "user_id": user.id,
                    "title": subject or "Message",
                    "body": text,
                    "data": {"communication_id": communication.id, "message_type": message_type},
                }
            )

    return ids, skipped, actions


async def _dispatch_message_actions(
    session: AsyncSession,
    *,
    tenant_slug: str,
    arq_pool: ArqRedis | None,
    actions: list[dict[str, Any]],
) -> None:
    if arq_pool is None:
        return
    for action in actions:
        if action["type"] == "email":
            await arq_pool.enqueue_job(
                "send_customer_communication_email",
                tenant_slug=tenant_slug,
                communication_id=action["communication_id"],
            )
        elif action["type"] == "push":
            await notify_user(
                session=session,
                tenant_slug=tenant_slug,
                user_id=action["user_id"],
                event="customer.message",
                title=action["title"],
                body=action["body"],
                data=action["data"],
                redis=arq_pool,
            )


async def send_customer_message(
    session: AsyncSession,
    *,
    tenant_slug: str,
    customer_id: int,
    body: CustomerMessageSendRequest,
    actor: dict,
    arq_pool: ArqRedis | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    user = await get_customer(session, customer_id)
    ids, skipped, actions = await _send_to_user(
        session,
        tenant_slug=tenant_slug,
        user=user,
        body=body,
        actor_user_id=int(actor["id"]),
        arq_pool=arq_pool,
    )
    await record_admin_audit(
        session,
        actor=actor,
        action="customer_message_sent",
        target_type="customer",
        target_id=customer_id,
        metadata={"channels": body.channels, "message_type": body.message_type, "communication_ids": ids},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await _dispatch_message_actions(session, tenant_slug=tenant_slug, arq_pool=arq_pool, actions=actions)
    return {"created": len(ids), "skipped": skipped, "communication_ids": ids}


async def _bulk_users(session: AsyncSession, body: BulkCustomerMessageRequest) -> list[User]:
    filters = _customer_filters(
        query=body.query,
        is_active=body.is_active,
        email_verified=body.email_verified,
        marketing_email_opt_in=body.marketing_email_opt_in,
        marketing_push_opt_in=body.marketing_push_opt_in,
    )
    if body.customer_ids:
        filters.append(User.id.in_(body.customer_ids))
    result = await session.execute(select(User).where(*filters).order_by(User.id.asc()))
    return list(result.scalars())


async def send_bulk_customer_message(
    session: AsyncSession,
    *,
    tenant_slug: str,
    body: BulkCustomerMessageRequest,
    actor: dict,
    arq_pool: ArqRedis | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    users = await _bulk_users(session, body)
    ids: list[int] = []
    actions: list[dict[str, Any]] = []
    skipped = 0
    for user in users:
        created_ids, skipped_count, user_actions = await _send_to_user(
            session,
            tenant_slug=tenant_slug,
            user=user,
            body=body,
            actor_user_id=int(actor["id"]),
            arq_pool=arq_pool,
        )
        ids.extend(created_ids)
        actions.extend(user_actions)
        skipped += skipped_count

    await record_admin_audit(
        session,
        actor=actor,
        action="customer_bulk_message_sent",
        target_type="customer_segment",
        target_id="bulk",
        metadata={"customer_count": len(users), "channels": body.channels, "communication_ids": ids},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    await _dispatch_message_actions(session, tenant_slug=tenant_slug, arq_pool=arq_pool, actions=actions)
    return {"created": len(ids), "skipped": skipped, "communication_ids": ids}


async def privacy_export(session: AsyncSession, customer_id: int) -> dict:
    user = await get_customer(session, customer_id)
    summaries = await _customer_summary(session, [customer_id])
    orders_result = await session.execute(
        select(Order).where(Order.user_id == customer_id).order_by(Order.created_at.desc(), Order.id.desc())
    )
    orders = [
        await orders_service.get_order_detail(session, order.id, user_id=customer_id, is_staff=True)
        for order in orders_result.scalars()
    ]
    communications_result = await session.execute(
        select(CustomerCommunication)
        .where(CustomerCommunication.user_id == customer_id)
        .order_by(CustomerCommunication.created_at.desc(), CustomerCommunication.id.desc())
    )
    return {
        "profile": _build_customer_item(user, summaries.get(customer_id)),
        "orders": orders,
        "communications": list(communications_result.scalars()),
        "exported_at": datetime.now(timezone.utc),
    }


async def deactivate_customer(
    session: AsyncSession,
    *,
    customer_id: int,
    reason: str,
    actor: dict,
    redis: ArqRedis | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    user = await get_customer(session, customer_id)
    user.is_active = False
    await session.execute(
        update(RefreshToken)
        .where(and_(RefreshToken.user_id == customer_id, RefreshToken.revoked_at.is_(None)))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await record_admin_audit(
        session,
        actor=actor,
        action="customer_privacy_delete",
        target_type="customer",
        target_id=customer_id,
        metadata={"reason": reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()

    if redis is not None:
        await flag_user_disabled(redis, customer_id, actor["tenant_slug"])
        await publish_session_revoked(redis, customer_id, actor["tenant_slug"], reason="customer_privacy_delete")


async def anonymize_customer(
    session: AsyncSession,
    *,
    customer_id: int,
    reason: str,
    actor: dict,
    redis: ArqRedis | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    user = await get_customer(session, customer_id)
    anonymized_email = f"anon-{customer_id}@deleted.local"
    user.email = anonymized_email
    user.full_name = "Client anonymise"
    user.phone = None
    user.is_active = False
    user.marketing_email_opt_in = False
    user.marketing_push_opt_in = False
    await session.execute(
        update(Order)
        .where(Order.user_id == customer_id)
        .values(
            customer_email=anonymized_email,
            customer_name="Client anonymise",
            customer_phone=None,
        )
    )
    await session.execute(
        update(RefreshToken)
        .where(and_(RefreshToken.user_id == customer_id, RefreshToken.revoked_at.is_(None)))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await record_admin_audit(
        session,
        actor=actor,
        action="customer_anonymized",
        target_type="customer",
        target_id=customer_id,
        metadata={"reason": reason},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()

    if redis is not None:
        await flag_user_disabled(redis, customer_id, actor["tenant_slug"])
        await publish_session_revoked(redis, customer_id, actor["tenant_slug"], reason="customer_anonymized")
