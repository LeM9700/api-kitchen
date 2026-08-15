"""Tasks ARQ : transmission des commandes au hub POS, traitement des callbacks
de statut, et reconciliation des commandes jamais acquittees.

Voir docs/superpowers/specs/2026-08-13-pos-order-transmission-design.md pour
le design complet. Aucune de ces taches n'est jamais appelee pendant une
requete entrante -- push_order_to_hub est enqueue post-commit depuis
app/modules/orders/service.py, process_hub_order_callback depuis
app/modules/pos/router.py::order_webhook, et reconcile_hub_orders est un cron.
"""
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import get_public_session, tenant_schema_name
from app.modules.catalog.sync_guards import acquire_sync_lock, check_rate_limit, release_sync_lock
from app.modules.notifications.notification_service import notify_staff
from app.modules.orders import hub_client
from app.modules.orders.hub_status import apply_hub_status
from app.modules.orders.models import Order, OrderHubTransmission
from app.modules.pos import service as pos_service
from app.modules.pos import webhook_service
from worker.tasks.worker_utils import with_dead_letter

logger = logging.getLogger(__name__)

# Cle de verrou dediee a la reconciliation (pas de connexion associee -- cf.
# sync_guards.acquire_sync_lock, qui ne fait que formatter la cle en string).
_RECONCILE_LOCK_KEY = -1
# Inferieur a la periodicite du cron (5 min) : le verrou ne doit jamais
# survivre a l'invocation qui l'a pose.
_RECONCILE_LOCK_TTL_SECONDS = 280


class HubOrderCallbackPayload(BaseModel):
    """Payload callback hub -- format hypothese, a confirmer avec le vrai fournisseur."""

    event_id: str
    external_establishment_id: str
    status: str
    private_reference: str | None = None
    hub_order_id: str | None = None


@with_dead_letter
async def push_order_to_hub(ctx, order_id: int, tenant_slug: str) -> None:
    """Transmet une commande au hub POS, en tache de fond.

    Jamais appelee dans le chemin critique d'une requete utilisateur --
    enqueue post-commit par ``orders/service.py``. Idempotent : si la
    commande a deja ete transmise (``transmission_status`` sent/acknowledged),
    ne fait rien.

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``, ``job_try``).
        order_id: Cle primaire de la commande a transmettre (schema tenant).
        tenant_slug: Slug du tenant proprietaire de la commande.
    """
    if not hub_client.is_configured():
        logger.info("push_order_to_hub: hub non configure, ignore order_id=%s tenant=%s", order_id, tenant_slug)
        return

    connection = await pos_service.get_active_connection(tenant_slug)
    if connection is None:
        logger.info("push_order_to_hub: aucune connexion POS active tenant=%s order_id=%s", tenant_slug, order_id)
        return

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)
    try:
        session = session_factory()
        try:
            await session.execute(text(f'SET search_path TO "{schema}", public'))
            order = await session.get(Order, order_id)
            if order is None:
                logger.warning("push_order_to_hub: order_id=%s introuvable tenant=%s", order_id, tenant_slug)
                return

            transmission = await session.scalar(
                select(OrderHubTransmission).where(OrderHubTransmission.order_id == order_id)
            )
            if transmission is None:
                transmission = OrderHubTransmission(order_id=order_id)
                session.add(transmission)
                await session.flush()

            if transmission.transmission_status in ("sent", "acknowledged"):
                logger.info(
                    "push_order_to_hub: deja transmis order_id=%s status=%s",
                    order_id,
                    transmission.transmission_status,
                )
                return

            # [SECURITE] private_reference reutilise l'Idempotency-Key deja
            # obligatoire sur la commande -- aucune nouvelle cle n'est generee.
            private_reference = order.idempotency_key or str(order.id)
            try:
                access_token = hub_client.decrypt_access_token(connection["access_token_encrypted"])
                client = hub_client.HttpHubOrderClient()
                result = await client.push_order(order, private_reference, access_token)
            except Exception as exc:
                transmission.transmission_status = "failed"
                transmission.last_error = type(exc).__name__
                await session.commit()
                logger.error(
                    "push_order_to_hub: echec envoi order_id=%s error_type=%s", order_id, type(exc).__name__
                )
                # [SECURITE] cf. sync_catalog_from_hub : seul le nom du type est
                # propage, jamais le message d'origine (peut contenir des
                # fragments de la reponse hub).
                raise RuntimeError(f"push_order_to_hub: echec envoi ({type(exc).__name__})") from None

            transmission.transmission_status = "sent"
            transmission.hub_order_id = result.hub_order_id
            transmission.sent_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info("push_order_to_hub: succes order_id=%s hub_order_id=%s", order_id, result.hub_order_id)
        finally:
            await session.close()
    finally:
        await engine.dispose()


@with_dead_letter
async def process_hub_order_callback(ctx, raw_body: str) -> None:
    """Traite un callback de statut hub, mis en file brut par le webhook.

    Idempotent sur deux axes independants :
    - callback duplique (meme ``event_id``) : detecte par contrainte
      d'unicite sur ``processed_hub_order_events``.
    - callback desordonne (statut plus ancien que celui deja connu) : delegue
      a ``apply_hub_status``, commun avec la reconciliation.

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``, ``job_try``).
        raw_body: Corps brut (str) du callback, jamais parse avant ce point
            (cf. app/modules/pos/router.py::order_webhook).
    """
    try:
        payload = HubOrderCallbackPayload.model_validate_json(raw_body)
    except ValidationError:
        logger.error("process_hub_order_callback: payload invalide, corps ignore")
        return

    context = await webhook_service.resolve_order_context(payload.external_establishment_id)
    if context is None:
        logger.warning("process_hub_order_callback: etablissement inconnu event_id=%s", payload.event_id)
        return

    tenant_slug = context["tenant_slug"]
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)
    try:
        session = session_factory()
        try:
            await session.execute(text(f'SET search_path TO "{schema}", public'))

            dedup_result = await session.execute(
                text(
                    "INSERT INTO processed_hub_order_events (event_id) VALUES (:event_id) "
                    "ON CONFLICT (event_id) DO NOTHING"
                ),
                {"event_id": payload.event_id},
            )
            if dedup_result.rowcount == 0:
                logger.info("process_hub_order_callback: duplicate_event event_id=%s", payload.event_id)
                await session.commit()
                return

            order = None
            if payload.private_reference:
                order = await session.scalar(
                    select(Order).where(Order.idempotency_key == payload.private_reference)
                )
            if order is None and payload.hub_order_id:
                transmission = await session.scalar(
                    select(OrderHubTransmission).where(
                        OrderHubTransmission.hub_order_id == payload.hub_order_id
                    )
                )
                if transmission is not None:
                    order = await session.get(Order, transmission.order_id)

            if order is None:
                logger.warning(
                    "process_hub_order_callback: commande introuvable event_id=%s tenant=%s",
                    payload.event_id,
                    tenant_slug,
                )
                await session.commit()
                return

            await session.execute(
                text("UPDATE processed_hub_order_events SET order_id = :order_id WHERE event_id = :event_id"),
                {"order_id": order.id, "event_id": payload.event_id},
            )

            await apply_hub_status(
                session,
                order.id,
                payload.status,
                tenant_slug=tenant_slug,
                hub_order_id=payload.hub_order_id,
                source="callback",
                arq_pool=ctx.get("redis"),
            )
        finally:
            await session.close()
    finally:
        await engine.dispose()


async def reconcile_hub_orders(ctx) -> None:
    """Cron ARQ : rattrape les commandes jamais acquittees par le hub.

    Respecte la limite de requetes/minute par connexion (``sync_guards``,
    reutilisee telle quelle) et n'alerte staff qu'une seule fois par commande
    (``order_hub_transmissions.alerted_at``).

    Args:
        ctx: Contexte ARQ injecte automatiquement (``redis``).
    """
    redis = ctx.get("redis")
    if not hub_client.is_status_configured():
        logger.info("reconcile_hub_orders: hub non configure (pos_hub_order_status_url vide), cron ignore")
        return

    if redis is not None:
        locked = await acquire_sync_lock(redis, _RECONCILE_LOCK_KEY, _RECONCILE_LOCK_TTL_SECONDS)
        if not locked:
            logger.info("reconcile_hub_orders: reconciliation deja en cours")
            return

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ack_timeout = timedelta(minutes=settings.pos_hub_order_ack_timeout_minutes)

    try:
        async with get_public_session() as public_session:
            tenants_result = await public_session.execute(text("SELECT slug FROM public.tenants"))
            tenant_slugs = [row[0] for row in tenants_result]

        for tenant_slug in tenant_slugs:
            connection = await pos_service.get_active_connection(tenant_slug)
            if connection is None:
                continue

            schema = tenant_schema_name(tenant_slug)
            session = session_factory()
            try:
                await session.execute(text(f'SET search_path TO "{schema}", public'))
                candidates = await session.execute(
                    select(OrderHubTransmission, Order)
                    .join(Order, Order.id == OrderHubTransmission.order_id)
                    .where(
                        OrderHubTransmission.transmission_status.in_(("pending", "sent")),
                        Order.status == "pending",
                    )
                )
                for transmission, order in candidates.all():
                    reference_time = transmission.sent_at or order.created_at
                    if reference_time is None:
                        continue
                    age = datetime.now(timezone.utc) - reference_time
                    if age < ack_timeout:
                        continue

                    if redis is not None:
                        allowed = await check_rate_limit(
                            redis, connection["id"], settings.pos_hub_order_status_rate_limit_per_minute
                        )
                        if not allowed:
                            logger.info(
                                "reconcile_hub_orders: rate limit atteint tenant=%s, reprise au prochain cron",
                                tenant_slug,
                            )
                            break

                    try:
                        access_token = hub_client.decrypt_access_token(connection["access_token_encrypted"])
                        client = hub_client.HttpHubOrderClient()
                        status_result = await client.fetch_status(
                            transmission.hub_order_id, order.idempotency_key or str(order.id), access_token
                        )
                    except Exception as exc:
                        logger.error(
                            "reconcile_hub_orders: echec fetch_status order_id=%s error_type=%s",
                            order.id,
                            type(exc).__name__,
                        )
                        continue

                    if status_result is not None:
                        await apply_hub_status(
                            session,
                            order.id,
                            status_result.status,
                            tenant_slug=tenant_slug,
                            hub_order_id=status_result.hub_order_id,
                            source="reconciliation",
                            arq_pool=redis,
                        )
                        continue

                    if transmission.alerted_at is None:
                        await notify_staff(
                            session,
                            tenant_slug,
                            "order.hub_never_acknowledged",
                            "Commande jamais confirmee par la caisse",
                            f"La commande #{order.id} n'a jamais ete confirmee par le hub POS.",
                            {"order_id": order.id},
                            redis=redis,
                        )
                        transmission.alerted_at = datetime.now(timezone.utc)
                        await session.commit()
            finally:
                await session.close()
    finally:
        await engine.dispose()
        if redis is not None:
            await release_sync_lock(redis, _RECONCILE_LOCK_KEY)
