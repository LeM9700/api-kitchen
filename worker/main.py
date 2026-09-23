from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from worker.tasks.catalog_sync import sync_catalog_from_hub, sync_stale_catalog_connections
from worker.tasks.emails import (
    notify_config_change,
    send_customer_communication_email,
    send_email,
    send_password_reset_email,
    send_stock_alert_email,
    send_verification_email,
)
from worker.tasks.fx_rates_sync import refresh_fx_rates
from worker.tasks.haccp_alerts import check_haccp_cooling_alerts, check_haccp_nc_alerts
from worker.tasks.hr_alerts import (
    check_labor_cost_risk,
    check_weekly_overtime,
    send_hr_late_alert,
    send_hr_overrun_alert,
)
from worker.tasks.loyalty import expire_loyalty_points
from worker.tasks.order_expiration import cancel_stale_pending_orders
from worker.tasks.scheduled_closures import process_scheduled_closures
from worker.tasks.stats import aggregate_live_stats, aggregate_monthly_stats
from worker.tasks.stats import aggregate_daily_stats
from worker.tasks.stock_alerts import send_stock_alert
from worker.tasks.stock_snapshot import aggregate_stock_snapshot
from worker.tasks.worker_utils import dead_letter_handler


def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.arq_redis_url)


class WorkerSettings:
    functions: ClassVar[list] = [
        send_stock_alert,
        send_hr_late_alert,
        send_hr_overrun_alert,
        send_email,
        send_customer_communication_email,
        send_verification_email,
        send_password_reset_email,
        send_stock_alert_email,
        notify_config_change,
        aggregate_daily_stats,
        dead_letter_handler,
        process_scheduled_closures,
        aggregate_stock_snapshot,
        sync_catalog_from_hub,
        sync_stale_catalog_connections,
    ]
    # Cron jobs ARQ : les fonctions recoivent uniquement ctx (pas de parametres dynamiques).
    cron_jobs: ClassVar[list] = [
        cron(aggregate_monthly_stats, hour=0, minute=0),
        cron(
            aggregate_live_stats,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        # [⚠️ PROD] Expire les points de fidélité obsolètes — 3h00 UTC pour éviter
        # de concurrencer le pic du soir. Timeout étendu à 600s (au lieu des 120s
        # globaux ci-dessous) pour les tenants avec beaucoup d'utilisateurs.
        cron(expire_loyalty_points, hour=3, minute=0, timeout=600),
        cron(aggregate_stock_snapshot, hour=set(range(24)), minute={0}),
        cron(check_weekly_overtime, hour=2, minute=0),
        cron(check_labor_cost_risk, hour=3, minute=0),
        # HACCP : alerte refroidissement toutes les 15 min (seuil légal 2h).
        cron(
            check_haccp_cooling_alerts,
            minute={0, 15, 30, 45},
        ),
        # HACCP : alerte NC en retard — 8h00 quotidien (début de service).
        cron(check_haccp_nc_alerts, hour=8, minute=0),
        cron(
            process_scheduled_closures,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(cancel_stale_pending_orders, minute=set(range(60))),
        # Synchronisation catalogue POS (hub) — filet de securite horaire, en
        # complement du webhook et de la resynchronisation paresseuse sur
        # snapshot perime (HubCatalogProvider.get_catalog).
        cron(sync_stale_catalog_connections, hour=set(range(24)), minute={0}),
        # Taux de change indicatifs (affichage multi-devise catalogue) —
        # rafraichissement quotidien, TTL cache 26h (voir fx_rates.py).
        cron(refresh_fx_rates, hour=4, minute=0, timeout=120),
    ]
    redis_settings = get_redis_settings()
    on_startup = None
    on_shutdown = None

    # Retry/backoff : 3 tentatives max, timeout 120s par job.
    max_tries = 3
    job_timeout = 120
