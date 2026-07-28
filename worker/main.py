from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from worker.tasks.emails import notify_config_change
from worker.tasks.hr_alerts import check_weekly_overtime
from worker.tasks.loyalty import expire_loyalty_points
from worker.tasks.scheduled_closures import process_scheduled_closures
from worker.tasks.stats import aggregate_live_stats, aggregate_monthly_stats
from worker.tasks.stock_snapshot import aggregate_stock_snapshot


def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.arq_redis_url)


class WorkerSettings:
    functions = [
        "worker.tasks.stock_alerts.send_stock_alert",
        "worker.tasks.hr_alerts.send_hr_late_alert",
        "worker.tasks.hr_alerts.send_hr_overrun_alert",
        "worker.tasks.emails.send_email",
        "worker.tasks.emails.send_verification_email",
        "worker.tasks.emails.send_password_reset_email",
        "worker.tasks.emails.send_stock_alert_email",
        "worker.tasks.emails.notify_config_change",
        "worker.tasks.stats.aggregate_daily_stats",
        "worker.tasks.worker_utils.dead_letter_handler",
        process_scheduled_closures,
        aggregate_stock_snapshot,
    ]
    # Cron jobs ARQ : les fonctions recoivent uniquement ctx (pas de parametres dynamiques).
    cron_jobs = [
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
        cron(
            process_scheduled_closures,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
    ]
    redis_settings = get_redis_settings()
    on_startup = None
    on_shutdown = None

    # Retry/backoff : 3 tentatives max, timeout 120s par job.
    max_tries = 3
    job_timeout = 120
