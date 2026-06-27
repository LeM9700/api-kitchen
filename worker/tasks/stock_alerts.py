import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.admin.tenants.models import TenantConfig
from app.modules.stock.models import Ingredient

try:
    from app.modules.notifications.notification_service import notify_staff
except Exception:  # pragma: no cover - defensive import for worker bootstrap/tests
    notify_staff = None

logger = logging.getLogger(__name__)

_DEFAULT_ALERT_COOLDOWN_HOURS = 4


async def send_stock_alert(
    ctx,
    ingredient_id: int,
    ingredient_name: str,
    current_qty: float,
    tenant_slug: str,
) -> None:
    """Task ARQ : evalue le rate-limit puis enqueue send_stock_alert_email si eligible.

    Le rate-limit est implemente via last_alert_sent_at sur l'ingredient :
    si une alerte a ete envoyee pendant la fenetre configuree par tenant_config,
    on ignore.

    Apres le commit de last_alert_sent_at, envoie egalement les notifications push
    aux utilisateurs staff/admin du tenant via notify_staff.

    [PROD] Le worker etant un processus separe du serveur FastAPI, broadcast_to_user
    ne touche pas les connexions WebSocket de l'API -- seul le push APNs/FCM est
    effectif depuis ce contexte.

    Args:
        ctx: Contexte ARQ injecte automatiquement.
        ingredient_id: Cle primaire de l'ingredient en alerte.
        ingredient_name: Nom de l'ingredient (pour le log).
        current_qty: Quantite courante au moment de l'alerte.
        tenant_slug: Slug du tenant pour router vers le bon schema.
    """
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)

    try:
        async with session_factory() as session:
            await session.execute(text(f'SET search_path TO "{schema}", public'))
            ingredient = await session.get(Ingredient, ingredient_id)
            if ingredient is None:
                logger.warning(
                    "send_stock_alert: ingredient_id=%s introuvable dans tenant=%s",
                    ingredient_id,
                    tenant_slug,
                )
                return

            now = datetime.now(timezone.utc)
            config = await session.get(TenantConfig, 1)
            cooldown_hours = (
                config.stock_alert_cooldown_hours
                if config is not None
                else _DEFAULT_ALERT_COOLDOWN_HOURS
            )
            cooldown_until = (
                ingredient.last_alert_sent_at + timedelta(hours=cooldown_hours)
                if ingredient.last_alert_sent_at
                else None
            )

            if cooldown_until is not None and now < cooldown_until:
                logger.info(
                    "STOCK ALERT supprimee (rate-limit %sh) ingredient=%s tenant=%s",
                    cooldown_hours,
                    ingredient_name,
                    tenant_slug,
                )
                return

            # Marquer last_alert_sent_at avant d'envoyer (evite doublons en cas de retry).
            ingredient.last_alert_sent_at = now
            await session.commit()

            # Notifications push staff (session reutilisee apres commit).
            # Le search_path reste valide tant qu'on est dans ce bloc session.
            try:
                if notify_staff is not None:
                    await notify_staff(
                        session=session,
                        tenant_slug=tenant_slug,
                        event="stock.low_alert",
                        title="Stock bas : " + ingredient_name,
                        body=ingredient_name + " : " + str(current_qty) + " unite(s) restante(s).",
                        data={
                            "ingredient_id": ingredient_id,
                            "ingredient_name": ingredient_name,
                            "current_qty": current_qty,
                        },
                    )
            except Exception as exc:
                logger.error(
                    "notify_staff failed for stock alert tenant=%s ingredient=%s: %s",
                    tenant_slug,
                    ingredient_name,
                    exc,
                )

        # Enqueue l'email reel dans le worker.
        arq_pool = ctx.get("redis")
        if arq_pool is not None:
            await arq_pool.enqueue_job(
                "send_stock_alert_email",
                tenant_slug=tenant_slug,
                ingredient_id=ingredient_id,
                ingredient_name=ingredient_name,
                current_qty=current_qty,
            )
        else:
            logger.warning(
                "[%s] STOCK ALERT: %s (id=%s) stock bas: %s (arq_pool indisponible)",
                tenant_slug,
                ingredient_name,
                ingredient_id,
                current_qty,
            )
    finally:
        await engine.dispose()
