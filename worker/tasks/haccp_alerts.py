"""Tasks ARQ — alertes HACCP en temps réel.

Deux cron jobs :
- ``check_haccp_cooling_alerts``  (toutes les 15 min)
  → Refroidissements actifs approchant ou dépassant les 2h réglementaires.
  → Alerte warning à 90 min, critique à 120 min.
- ``check_haccp_nc_alerts``       (quotidien à 8h00)
  → Non-conformités ouvertes ou en cours depuis > 24h.

[🔒 SÉCURITÉ] Les données émises via notify_staff ne contiennent pas
d'informations personnelles identifiables (nom de produit seulement).

[⚠️ PROD] Le cron tourne dans le processus worker ARQ, séparé des
instances FastAPI. broadcast_to_user émet dans Redis pub/sub →
toutes les instances relaient via WebSocket aux clients connectés.
"""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.haccp.models import HaccpCoolingLog, HaccpNonConformity
from worker.tasks.stats import _get_all_tenant_slugs

try:
    from app.modules.notifications.notification_service import notify_staff
except Exception:  # pragma: no cover – defensive import pour bootstrap/tests
    notify_staff = None

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

_COOLING_WARNING_MINUTES = 90   # Seuil d'alerte préventive (GEMRCN)
_COOLING_CRITICAL_MINUTES = 120  # Limite légale (10°C en 2h)
_NC_OVERDUE_HOURS = 24           # NC non traitée considérée comme en retard

_EVENT_COOLING_WARNING = "haccp.cooling_warning"
_EVENT_COOLING_CRITICAL = "haccp.cooling_critical"
_EVENT_NC_OVERDUE = "haccp.nc_overdue"


@asynccontextmanager
async def _open_tenant_session(engine, tenant_slug: str):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)
    async with session_factory() as session:
        await session.execute(text(f'SET search_path TO "{schema}", public'))
        yield session


# ── Refroidissement rapide ────────────────────────────────────────────────────

async def check_haccp_cooling_alerts(ctx) -> None:
    """Cron ARQ — vérifie les refroidissements actifs pour tous les tenants.

    Émet deux niveaux d'alerte :
    - ``haccp.cooling_warning``  : refroidissement en cours depuis 90 min
    - ``haccp.cooling_critical`` : refroidissement en cours depuis >= 120 min

    Le job n'a pas de cooldown explicite : si un refroidissement reste actif
    au-delà de 2h, une alerte critique est émise à chaque exécution (15 min)
    jusqu'à ce que ``ended_at`` soit renseigné.

    [⚠️ PROD] Une alerte critique à 120 min sans action doit conduire à
    jeter le produit (DDPP). Le message le mentionne explicitement.
    """
    if notify_staff is None:
        logger.warning("check_haccp_cooling_alerts: notify_staff non disponible")
        return

    engine = create_async_engine(settings.database_url)
    now = datetime.now(UTC)
    warning_threshold = now - timedelta(minutes=_COOLING_WARNING_MINUTES)
    critical_threshold = now - timedelta(minutes=_COOLING_CRITICAL_MINUTES)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)

        for slug in tenant_slugs:
            try:
                async with _open_tenant_session(engine, slug) as session:
                    # Tous les refroidissements actifs démarrés il y a >= 90 min
                    result = await session.execute(
                        select(HaccpCoolingLog).where(
                            and_(
                                HaccpCoolingLog.ended_at.is_(None),
                                HaccpCoolingLog.started_at <= warning_threshold,
                            )
                        )
                    )
                    active_logs = result.scalars().all()

                    for log in active_logs:
                        elapsed_min = (now - log.started_at).total_seconds() / 60
                        is_critical = log.started_at <= critical_threshold

                        event = _EVENT_COOLING_CRITICAL if is_critical else _EVENT_COOLING_WARNING
                        elapsed_str = f"{int(elapsed_min)} min"

                        if is_critical:
                            title = f"⚠️ Refroidissement critique — {log.product_name}"
                            body = (
                                f"{log.product_name} est en refroidissement depuis {elapsed_str}. "
                                "Limite légale de 2h dépassée. Vérifier ou éliminer le produit."
                            )
                        else:
                            title = f"⏱ Refroidissement — {log.product_name}"
                            body = (
                                f"{log.product_name} est en refroidissement depuis {elapsed_str}. "
                                "Vérifier la température avant 120 min."
                            )

                        try:
                            await notify_staff(
                                session=session,
                                tenant_slug=slug,
                                event=event,
                                title=title,
                                body=body,
                                data={
                                    "cooling_log_id": log.id,
                                    "product_name": log.product_name,
                                    "started_at": log.started_at.isoformat(),
                                    "temp_start": log.temp_start,
                                    "elapsed_minutes": int(elapsed_min),
                                    "is_critical": is_critical,
                                },
                            )
                        except Exception as exc:
                            logger.error(
                                "cooling alert notify_staff failed tenant=%s log_id=%s: %s",
                                slug, log.id, exc,
                            )

            except Exception as exc:
                logger.error(
                    "check_haccp_cooling_alerts: erreur tenant=%s : %s", slug, exc
                )

    finally:
        await engine.dispose()


# ── Non-conformités en retard ─────────────────────────────────────────────────

async def check_haccp_nc_alerts(ctx) -> None:
    """Cron ARQ — signale les non-conformités ouvertes depuis > 24h.

    Une NC ouvertes ou en cours depuis plus de 24h sans action manager
    représente un risque réglementaire (manque de traçabilité DDPP).

    [⚠️ PROD] Ce job ne génère qu'une alerte par NC en retard, chaque jour,
    sans deduplication inter-run — acceptable pour un job quotidien.
    """
    if notify_staff is None:
        logger.warning("check_haccp_nc_alerts: notify_staff non disponible")
        return

    engine = create_async_engine(settings.database_url)
    now = datetime.now(UTC)
    overdue_threshold = now - timedelta(hours=_NC_OVERDUE_HOURS)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)

        for slug in tenant_slugs:
            try:
                async with _open_tenant_session(engine, slug) as session:
                    result = await session.execute(
                        select(HaccpNonConformity).where(
                            and_(
                                HaccpNonConformity.status.in_(["open", "in_progress"]),
                                HaccpNonConformity.created_at <= overdue_threshold,
                            )
                        )
                    )
                    overdue_ncs = result.scalars().all()

                    if not overdue_ncs:
                        continue

                    nc_count = len(overdue_ncs)
                    title = (
                        f"⚠️ {nc_count} non-conformité{'s' if nc_count > 1 else ''} en retard"
                    )
                    body = (
                        f"{nc_count} NC sans traitement depuis plus de 24h. "
                        "Une action corrective validée est requise."
                    )

                    try:
                        await notify_staff(
                            session=session,
                            tenant_slug=slug,
                            event=_EVENT_NC_OVERDUE,
                            title=title,
                            body=body,
                            data={
                                "nc_count": nc_count,
                                "nc_ids": [nc.id for nc in overdue_ncs],
                                "oldest_nc_hours": int(
                                    (now - min(nc.created_at for nc in overdue_ncs)).total_seconds()
                                    / 3600
                                ),
                            },
                        )
                    except Exception as exc:
                        logger.error(
                            "nc_overdue alert notify_staff failed tenant=%s: %s", slug, exc
                        )

            except Exception as exc:
                logger.error(
                    "check_haccp_nc_alerts: erreur tenant=%s : %s", slug, exc
                )

    finally:
        await engine.dispose()
