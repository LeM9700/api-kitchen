import logging
from datetime import datetime, timedelta, timezone

import anyio
import stripe
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import engine, get_tenant_session
from app.modules.admin.customers.models import AdminAuditLog, CustomerCommunication
from app.modules.notifications.notification_service import notify_user
from app.modules.orders.models import Order, OrderStatusHistory
from app.modules.payments.models import Payment

logger = logging.getLogger(__name__)

PENDING_ORDER_TIMEOUT_MINUTES = 5


async def _active_tenant_slugs() -> list[str]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            text("SELECT slug FROM public.tenants WHERE COALESCE(is_suspended, false) = false")
        )
        return [row[0] for row in result]


async def cancel_stale_pending_orders(ctx) -> None:
    """Annule les commandes non payees restees pending plus de 5 minutes."""
    redis = ctx.get("redis") if ctx else None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PENDING_ORDER_TIMEOUT_MINUTES)

    for tenant_slug in await _active_tenant_slugs():
        try:
            actions: list[dict] = []
            async with get_tenant_session(tenant_slug) as session:
                result = await session.execute(
                    select(Order)
                    .where(
                        Order.status == "pending",
                        Order.payment_status == "pending",
                        Order.created_at <= cutoff,
                    )
                    .order_by(Order.created_at.asc())
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
                orders = list(result.scalars())

                for order in orders:
                    order.status = "cancelled"
                    session.add(
                        OrderStatusHistory(
                            order_id=order.id,
                            status="cancelled",
                            note="Annulation automatique: commande en attente de paiement depuis plus de 5 minutes",
                            authority="internal",
                        )
                    )
                    session.add(
                        AdminAuditLog(
                            actor_user_id=None,
                            actor_email="system",
                            action="order_auto_cancelled",
                            target_type="order",
                            target_id=str(order.id),
                            metadata_json={
                                "reason": "pending_payment_timeout",
                                "timeout_minutes": PENDING_ORDER_TIMEOUT_MINUTES,
                            },
                        )
                    )

                    payments_result = await session.execute(
                        select(Payment).where(Payment.order_id == order.id, Payment.status == "pending")
                    )
                    pending_payments = list(payments_result.scalars())
                    for payment in pending_payments:
                        if payment.provider_payment_id and not payment.provider_payment_id.startswith("local_"):
                            try:
                                await anyio.to_thread.run_sync(
                                    lambda: stripe.PaymentIntent.cancel(
                                        payment.provider_payment_id,
                                        **(
                                            {"stripe_account": payment.provider_account_id}
                                            if payment.provider_account_id
                                            else {}
                                        ),
                                    )
                                )
                            except stripe.error.StripeError as exc:
                                logger.warning(
                                    "stale pending order: Stripe cancel failed tenant=%s order_id=%s payment_id=%s error=%s",
                                    tenant_slug,
                                    order.id,
                                    payment.id,
                                    exc,
                                )
                        payment.status = "expired"

                    message = (
                        "Votre commande a ete annulee car elle est restee en attente "
                        "de confirmation ou de paiement plus de 5 minutes."
                    )
                    if order.user_id is not None:
                        email_comm = CustomerCommunication(
                            user_id=order.user_id,
                            channel="email",
                            message_type="transactional",
                            template_key="order_auto_cancelled",
                            subject="Votre commande a ete annulee",
                            body=message,
                            status="queued",
                        )
                        push_comm = CustomerCommunication(
                            user_id=order.user_id,
                            channel="push",
                            message_type="transactional",
                            template_key="order_auto_cancelled",
                            subject="Commande annulee",
                            body=message,
                            status="sent",
                            sent_at=datetime.now(timezone.utc),
                        )
                        session.add_all([email_comm, push_comm])
                        await session.flush()
                        if redis is not None:
                            actions.append(
                                {
                                    "type": "email",
                                    "communication_id": email_comm.id,
                                }
                            )
                            actions.append(
                                {
                                    "type": "push",
                                    "user_id": order.user_id,
                                    "title": "Commande annulee",
                                    "body": message,
                                    "data": {"order_id": order.id, "communication_id": push_comm.id},
                                }
                            )

                if orders:
                    await session.commit()
                    if redis is not None:
                        for action in actions:
                            if action["type"] == "email":
                                await redis.enqueue_job(
                                    "send_customer_communication_email",
                                    tenant_slug=tenant_slug,
                                    communication_id=action["communication_id"],
                                )
                            elif action["type"] == "push":
                                await notify_user(
                                    session=session,
                                    tenant_slug=tenant_slug,
                                    user_id=action["user_id"],
                                    event="order.auto_cancelled",
                                    title=action["title"],
                                    body=action["body"],
                                    data=action["data"],
                                    redis=redis,
                                )

        except Exception as exc:
            logger.error("cancel_stale_pending_orders failed tenant=%s: %s", tenant_slug, exc)
