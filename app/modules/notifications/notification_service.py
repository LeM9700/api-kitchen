"""Service orchestrateur de notifications multi-canal (WebSocket + push).

Logique d'envoi :
1. Publication Redis pub/sub (si redis fourni) -> toutes les instances Railway
   recoivent via ws_router._redis_subscriber et diffusent a leurs WS locaux.
2. Push APNs/FCM systematique -> appareils background/fermes.
   Le client Flutter deduplique via notification_id dans data.

Les fonctions de ce module ne levent jamais d'exception : toute erreur est
loguee et l'appelant n'est pas impacte.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.notifications.push_service import send_push_notification
from app.modules.notifications.ws_router import broadcast_to_user

logger = logging.getLogger(__name__)

_STAFF_BATCH_SIZE: int = 20
_STAFF_CONCURRENCY: int = 10


async def notify_user(
    session: AsyncSession,
    tenant_slug: str,
    user_id: int,
    event: str,
    title: str,
    body: str,
    data: dict,
    redis=None,
) -> None:
    """Envoie une notification a un utilisateur sur tous ses canaux et appareils.

    Publie dans Redis pub/sub (si redis fourni) pour que toutes les instances
    Railway recoivent le message WebSocket via _redis_subscriber. Envoie ensuite
    le push APNs/FCM pour les appareils background/fermes. Le push est envoye
    meme si le WS a livre ; le client Flutter deduplique via notification_id.

    Si redis est None (tests, dev mono-instance), fallback sur broadcast local
    direct via broadcast_to_user.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        tenant_slug: Slug du tenant.
        user_id: Identifiant de l'utilisateur cible.
        event: Identifiant de l'evenement (ex: "order.confirmed").
        title: Titre de la notification.
        body: Corps de la notification.
        data: Donnees supplementaires (order_id, ingredient_id...).
        redis: Client Redis (app.state.arq_pool) pour la publication pub/sub
            multi-instance. None = broadcast local uniquement.
    """
    try:
        notification_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        enriched_data = {**data, "notification_id": notification_id}

        ws_message = {
            "type": "notification",
            "event": event,
            "title": title,
            "body": body,
            "data": enriched_data,
            "notification_id": notification_id,
            "timestamp": timestamp,
        }

        if redis is not None:
            # [PROD] Publication Redis -> toutes les instances Railway recoivent
            # via _redis_subscriber et dispatchent a leurs connexions WS locales.
            channel = f"notif:{tenant_slug}:{user_id}"
            await redis.publish(channel, json.dumps(ws_message))
            logger.debug(
                "Redis publish: channel=%s event=%s notification_id=%s",
                channel,
                event,
                notification_id,
            )
        else:
            # Fallback mono-instance (tests, dev sans Redis pub/sub).
            ws_count = await broadcast_to_user(tenant_slug, user_id, ws_message)
            if ws_count:
                logger.debug(
                    "WS broadcast (local): tenant=%s user_id=%s event=%s sockets=%d",
                    tenant_slug,
                    user_id,
                    event,
                    ws_count,
                )

        push_result = await send_push_notification(
            session=session,
            tenant_slug=tenant_slug,
            user_id=user_id,
            title=title,
            body=body,
            data=enriched_data,
        )
        logger.debug(
            "Push: tenant=%s user_id=%s event=%s sent=%d failed=%d",
            tenant_slug,
            user_id,
            event,
            push_result["sent"],
            push_result["failed"],
        )

    except Exception as exc:
        logger.error(
            "notify_user failed: tenant=%s user_id=%s event=%s: %s",
            tenant_slug,
            user_id,
            event,
            exc,
        )


async def notify_staff(
    session: AsyncSession,
    tenant_slug: str,
    event: str,
    title: str,
    body: str,
    data: dict,
    redis=None,
) -> None:
    """Envoie une notification a tous les utilisateurs staff et admin du tenant.

    Charge les users actifs en batches de 20 (LIMIT/OFFSET) et traite chaque
    batch en parallele avec un asyncio.Semaphore(10) pour limiter la concurrence
    sur la base et le reseau. Les erreurs individuelles n'interrompent pas la boucle.

    [PERF] Le batch de 20 + semaphore de 10 evite de charger toute la table en
    memoire et de saturer le pool de connexions DB/Redis en cas de nombreux staffs.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        tenant_slug: Slug du tenant.
        event: Identifiant de l'evenement (ex: "order.new", "stock.low_alert").
        title: Titre de la notification.
        body: Corps de la notification.
        data: Donnees supplementaires.
        redis: Client Redis pour pub/sub multi-instance (transmis a notify_user).
    """
    semaphore = asyncio.Semaphore(_STAFF_CONCURRENCY)

    async def _bounded_notify(uid: int) -> None:
        """Appelle notify_user avec la concurrence limitee par le semaphore.

        Args:
            uid: Identifiant de l'utilisateur a notifier.
        """
        async with semaphore:
            await notify_user(
                session=session,
                tenant_slug=tenant_slug,
                user_id=uid,
                event=event,
                title=title,
                body=body,
                data=data,
                redis=redis,
            )

    try:
        offset = 0
        while True:
            result = await session.execute(
                select(User)
                .where(
                    User.role.in_(("staff", "admin")),
                    User.is_active.is_(True),
                )
                .limit(_STAFF_BATCH_SIZE)
                .offset(offset)
            )
            batch = list(result.scalars())
            if not batch:
                break

            await asyncio.gather(*[_bounded_notify(u.id) for u in batch])
            offset += _STAFF_BATCH_SIZE

    except Exception as exc:
        logger.error(
            "notify_staff failed: tenant=%s event=%s: %s",
            tenant_slug,
            event,
            exc,
        )
