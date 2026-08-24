"""Router HACCP — statistiques agrégées (scorecard hebdomadaire).

Endpoint :
  GET /haccp/stats?from=YYYY-MM-DD&to=YYYY-MM-DD

Accessible par les utilisateurs admin et staff ayant la permission
``haccp_read``. Les données sont agrégées par période et incluent :
  - Taux de complétion des sessions (ouverture/fermeture)
  - Taux de conformité température, DLC, nettoyage
  - Bilan des non-conformités (open/in_progress/closed)
  - Taux de conformité réceptions fournisseurs
  - Taux de conformité refroidissements rapides
  - Score global pondéré

[⚠️ PROD] Toutes les requêtes sont exécutées dans le schéma tenant
courant — pas d'accès cross-tenant possible via cet endpoint.
"""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Integer, func, select

from app.core.database import get_tenant_session
from app.core.http.deps import require_permission
from app.modules.haccp.models import (
    HaccpCheckSession,
    HaccpCleaningLog,
    HaccpCleaningTask,
    HaccpCoolingLog,
    HaccpDlcCheck,
    HaccpNonConformity,
    HaccpReceptionControl,
    HaccpTemperatureLog,
)

router = APIRouter(prefix="/stats", tags=["haccp-stats"])


# ── Schémas de réponse ────────────────────────────────────────────────────────

class StatSection(BaseModel):
    """Statistiques conformité pour une catégorie."""

    compliant: int
    total: int
    compliance_rate: float  # 0.0 – 100.0


class SessionStats(BaseModel):
    opening_completed: int
    opening_total: int
    closing_completed: int
    closing_total: int
    completion_rate: float


class NcStats(BaseModel):
    open: int
    in_progress: int
    closed: int
    total: int
    resolution_rate: float  # closed / total


class CoolingStats(BaseModel):
    compliant: int
    non_compliant: int
    in_progress: int
    total: int
    compliance_rate: float


class HaccpStatsResponse(BaseModel):
    from_date: date
    to_date: date
    sessions: SessionStats
    temperature: StatSection
    dlc: StatSection
    cleaning: StatSection
    non_conformities: NcStats
    reception: StatSection
    cooling: CoolingStats
    overall_score: float  # Score pondéré 0–100


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rate(compliant: int, total: int) -> float:
    if total == 0:
        return 100.0
    return round(compliant / total * 100, 1)


def _overall_score(
    sessions: SessionStats,
    temperature: StatSection,
    dlc: StatSection,
    cleaning: StatSection,
    nc: NcStats,
    reception: StatSection,
    cooling: CoolingStats,
) -> float:
    """Score global pondéré — reflète la qualité du PMS sur la période.

    Pondérations :
      - Sessions complétées : 20 %  (gate bloquant — impact élevé)
      - Températures         : 25 %  (risque bactérien direct)
      - DLC                  : 20 %  (conformité réglementaire)
      - Nettoyage            : 15 %
      - NC résolues          : 10 %  (suivi correctif)
      - Réception            : 5 %
      - Refroidissement      : 5 %
    """
    weights = {
        "sessions": 0.20,
        "temperature": 0.25,
        "dlc": 0.20,
        "cleaning": 0.15,
        "nc": 0.10,
        "reception": 0.05,
        "cooling": 0.05,
    }
    scores = {
        "sessions": sessions.completion_rate,
        "temperature": temperature.compliance_rate,
        "dlc": dlc.compliance_rate,
        "cleaning": cleaning.compliance_rate,
        "nc": nc.resolution_rate,
        "reception": reception.compliance_rate,
        "cooling": cooling.compliance_rate,
    }
    total = sum(scores[k] * weights[k] for k in weights)
    return round(total, 1)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("", response_model=HaccpStatsResponse, summary="Scorecard HACCP")
async def get_haccp_stats(
    from_date: date = Query(..., alias="from", description="Date de début"),
    to_date: date = Query(..., alias="to", description="Date de fin"),
    current_user: dict = Depends(require_permission("haccp:read", "staff", "admin")),
) -> HaccpStatsResponse:
    """Retourne les statistiques HACCP agrégées pour la période donnée.

    Utilisé par le dashboard hebdomadaire — scorecard de conformité.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to >= from requis")

    async with get_tenant_session(current_user["tenant_slug"]) as db:
        return await _compute_stats(db, from_date, to_date)


async def _compute_stats(db, from_date: date, to_date: date) -> HaccpStatsResponse:
    # ── Sessions ──────────────────────────────────────────────────────────────
    all_sessions = (
        (await db.execute(
            select(HaccpCheckSession).where(
                HaccpCheckSession.date.between(from_date, to_date)
            )
        ))
        .scalars()
        .all()
    )
    opening_sessions = [s for s in all_sessions if s.session_type == "opening"]
    closing_sessions = [s for s in all_sessions if s.session_type == "closing"]
    opening_done = sum(1 for s in opening_sessions if s.status == "complete")
    closing_done = sum(1 for s in closing_sessions if s.status == "complete")
    session_total = len(opening_sessions) + len(closing_sessions)
    session_done = opening_done + closing_done
    sessions = SessionStats(
        opening_completed=opening_done,
        opening_total=len(opening_sessions),
        closing_completed=closing_done,
        closing_total=len(closing_sessions),
        completion_rate=_rate(session_done, session_total),
    )

    # IDs des sessions de la période (pour filtrer les logs)
    session_ids = [s.id for s in all_sessions]

    # ── Températures ──────────────────────────────────────────────────────────
    if session_ids:
        temp_rows = (
            (await db.execute(
                select(
                    func.count().label("total"),
                    func.sum(
                        HaccpTemperatureLog.is_compliant.cast(Integer)
                    ).label("compliant"),
                ).where(HaccpTemperatureLog.session_id.in_(session_ids))
            ))
            .one()
        )
        temp_total = temp_rows.total or 0
        temp_compliant = int(temp_rows.compliant or 0)
    else:
        temp_total = temp_compliant = 0

    temperature = StatSection(
        compliant=temp_compliant,
        total=temp_total,
        compliance_rate=_rate(temp_compliant, temp_total),
    )

    # ── DLC ───────────────────────────────────────────────────────────────────
    if session_ids:
        dlc_rows = (
            (await db.execute(
                select(
                    func.count().label("total"),
                    func.sum(
                        HaccpDlcCheck.is_compliant.cast(Integer)
                    ).label("compliant"),
                ).where(HaccpDlcCheck.session_id.in_(session_ids))
            ))
            .one()
        )
        dlc_total = dlc_rows.total or 0
        dlc_compliant = int(dlc_rows.compliant or 0)
    else:
        dlc_total = dlc_compliant = 0

    dlc = StatSection(
        compliant=dlc_compliant,
        total=dlc_total,
        compliance_rate=_rate(dlc_compliant, dlc_total),
    )

    # ── Nettoyage ─────────────────────────────────────────────────────────────
    # Nettoyages réalisés vs tâches attendues sur la période
    if session_ids:
        cleaning_done = (
            (await db.execute(
                select(func.count()).where(
                    HaccpCleaningLog.session_id.in_(session_ids)
                )
            ))
            .scalar()
            or 0
        )
        # Tâches actives × sessions (approximation : 1 tâche par session)
        active_tasks = (
            (await db.execute(
                select(func.count()).where(HaccpCleaningTask.is_active.is_(True))
            ))
            .scalar()
            or 0
        )
        cleaning_expected = active_tasks * len(all_sessions)
    else:
        cleaning_done = cleaning_expected = 0

    cleaning = StatSection(
        compliant=cleaning_done,
        total=cleaning_expected,
        compliance_rate=_rate(cleaning_done, cleaning_expected),
    )

    # ── Non-conformités ───────────────────────────────────────────────────────
    nc_start = datetime.combine(from_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    nc_end = datetime.combine(to_date, datetime.max.time()).replace(
        tzinfo=timezone.utc
    )
    nc_rows = (
        (await db.execute(
            select(
                HaccpNonConformity.status,
                func.count().label("cnt"),
            )
            .where(
                HaccpNonConformity.created_at.between(nc_start, nc_end)
            )
            .group_by(HaccpNonConformity.status)
        ))
        .all()
    )
    nc_map = {r.status: r.cnt for r in nc_rows}
    nc_open = nc_map.get("open", 0)
    nc_in_progress = nc_map.get("in_progress", 0)
    nc_closed = nc_map.get("closed", 0)
    nc_total = nc_open + nc_in_progress + nc_closed
    non_conformities = NcStats(
        open=nc_open,
        in_progress=nc_in_progress,
        closed=nc_closed,
        total=nc_total,
        resolution_rate=_rate(nc_closed, nc_total),
    )

    # ── Réceptions ────────────────────────────────────────────────────────────
    rec_rows = (
        (await db.execute(
            select(
                func.count().label("total"),
                func.sum(
                    HaccpReceptionControl.is_accepted.cast(Integer)
                ).label("accepted"),
            ).where(
                HaccpReceptionControl.delivery_date.between(from_date, to_date)
            )
        ))
        .one()
    )
    rec_total = rec_rows.total or 0
    rec_accepted = int(rec_rows.accepted or 0)
    reception = StatSection(
        compliant=rec_accepted,
        total=rec_total,
        compliance_rate=_rate(rec_accepted, rec_total),
    )

    # ── Refroidissement ───────────────────────────────────────────────────────
    cool_start = datetime.combine(from_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    cool_end = datetime.combine(to_date, datetime.max.time()).replace(
        tzinfo=timezone.utc
    )
    cool_all = (
        (await db.execute(
            select(HaccpCoolingLog).where(
                HaccpCoolingLog.started_at.between(cool_start, cool_end)
            )
        ))
        .scalars()
        .all()
    )
    cool_compliant = sum(1 for c in cool_all if c.is_compliant is True)
    cool_non_compliant = sum(1 for c in cool_all if c.is_compliant is False)
    cool_in_progress = sum(1 for c in cool_all if c.is_compliant is None)
    cool_finished = cool_compliant + cool_non_compliant
    cooling = CoolingStats(
        compliant=cool_compliant,
        non_compliant=cool_non_compliant,
        in_progress=cool_in_progress,
        total=len(cool_all),
        compliance_rate=_rate(cool_compliant, cool_finished),
    )

    overall = _overall_score(
        sessions, temperature, dlc, cleaning, non_conformities, reception, cooling
    )

    return HaccpStatsResponse(
        from_date=from_date,
        to_date=to_date,
        sessions=sessions,
        temperature=temperature,
        dlc=dlc,
        cleaning=cleaning,
        non_conformities=non_conformities,
        reception=reception,
        cooling=cooling,
        overall_score=overall,
    )
