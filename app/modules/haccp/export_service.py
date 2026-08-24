"""Service d'export HACCP — PDF (WeasyPrint) et CSV.

Génère des rapports conformes PMS (Plan de Maîtrise Sanitaire) couvrant :
- Sessions ouverture/fermeture
- Relevés de température
- Vérifications DLC 1/2/3
- Plan de nettoyage / désinfection
- Non-conformités et actions correctives
- Contrôles réception fournisseurs
- Refroidissement rapide
- Registre de formation hygiène

[⚠️ PROD] WeasyPrint est synchrone (appel système Cairo/Pango).
Appeler via ``asyncio.get_event_loop().run_in_executor`` pour ne pas
bloquer l'event loop FastAPI.
"""

import asyncio
import csv
import io
import os
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.haccp.models import (
    HaccpCheckSession,
    HaccpCleaningLog,
    HaccpCleaningTask,
    HaccpCoolingLog,
    HaccpDlcCheck,
    HaccpEquipment,
    HaccpNonConformity,
    HaccpReceptionControl,
    HaccpTemperatureLog,
    HaccpTrainingRecord,
)

# ── Chemin templates ──────────────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parents[3] / "app" / "templates" / "haccp"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)

# ── Labels ────────────────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "temperature": "Température",
    "dlc": "DLC",
    "cleaning": "Nettoyage",
    "reception": "Réception",
    "cooling": "Refroidissement",
}

_DLC_LEVEL_LABELS = {
    1: "DLC emballage",
    2: "DLC conservation",
    3: "DLC utilisation",
}

_TRAINING_TYPE_LABELS = {
    "hygiene_14h": "Hygiène alimentaire (14h obligatoires)",
    "refresher": "Formation de recyclage",
    "haccp_module": "HACCP / PMS",
    "other": "Autre",
}


def _fmt_date(d: date | datetime | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, datetime):
        return d.strftime("%d/%m/%Y %H:%M")
    return d.strftime("%d/%m/%Y")


def _fmt_datetime(d: datetime | None) -> str:
    if d is None:
        return "—"
    return d.strftime("%d/%m/%Y %H:%M")


# ── Collecte des données ──────────────────────────────────────────────────────

async def _collect_data(
    db: AsyncSession,
    from_date: date,
    to_date: date,
    restaurant_name: str,
) -> dict:
    """Récupère toutes les données HACCP pour la période donnée."""

    # Sessions
    sessions_q = (await db.execute(
        select(HaccpCheckSession).where(
            and_(
                HaccpCheckSession.date >= from_date,
                HaccpCheckSession.date <= to_date,
            )
        ).order_by(HaccpCheckSession.date, HaccpCheckSession.session_type)
    )).scalars().all()

    session_ids = [s.id for s in sessions_q]

    sessions = [
        {
            "date": _fmt_date(s.date),
            "session_type": s.session_type,
            "status": s.status,
            "completed_by": s.completed_by,
            "completed_at": _fmt_datetime(s.completed_at),
            "notes": s.notes,
        }
        for s in sessions_q
    ]

    # Équipements (pour enrichir les relevés T°)
    equipment_map: dict[int, HaccpEquipment] = {}
    if session_ids:
        equip_q = (await db.execute(select(HaccpEquipment))).scalars().all()
        equipment_map = {e.id: e for e in equip_q}

    # Relevés température
    temp_q = (
        (await db.execute(
            select(HaccpTemperatureLog, HaccpCheckSession.session_type)
            .join(HaccpCheckSession, HaccpTemperatureLog.session_id == HaccpCheckSession.id)
            .where(HaccpTemperatureLog.session_id.in_(session_ids))
            .order_by(HaccpTemperatureLog.logged_at)
        )).all()
        if session_ids else []
    )

    temperature_logs = []
    for row in temp_q:
        t, stype = row
        equip = equipment_map.get(t.equipment_id)
        target_range = "—"
        if equip and equip.target_min_temp is not None and equip.target_max_temp is not None:
            target_range = f"{equip.target_min_temp:.0f}°C – {equip.target_max_temp:.0f}°C"
        temperature_logs.append({
            "logged_at": _fmt_datetime(t.logged_at),
            "session_label": "Ouverture" if stype == "opening" else "Fermeture",
            "equipment_name": equip.name if equip else f"#{t.equipment_id}",
            "measured_temp": f"{t.measured_temp:.1f}",
            "target_range": target_range,
            "is_compliant": t.is_compliant,
            "corrective_action": t.corrective_action,
        })

    # DLC checks
    dlc_q = (
        (await db.execute(
            select(HaccpDlcCheck)
            .where(HaccpDlcCheck.session_id.in_(session_ids))
            .order_by(HaccpDlcCheck.logged_at)
        )).scalars().all()
        if session_ids else []
    )

    dlc_checks = [
        {
            "logged_at": _fmt_datetime(d.logged_at),
            "ingredient_name": d.ingredient_name,
            "level_label": _DLC_LEVEL_LABELS.get(d.dlc_level, f"Niveau {d.dlc_level}"),
            "dlc_date": _fmt_date(d.dlc_date),
            "is_compliant": d.is_compliant,
            "corrective_action": d.corrective_action,
        }
        for d in dlc_q
    ]

    # Nettoyage
    cleaning_task_map: dict[int, HaccpCleaningTask] = {}
    tasks_q = (await db.execute(select(HaccpCleaningTask))).scalars().all()
    cleaning_task_map = {t.id: t for t in tasks_q}

    cleaning_q = (
        (await db.execute(
            select(HaccpCleaningLog, HaccpCheckSession.session_type)
            .join(HaccpCheckSession, HaccpCleaningLog.session_id == HaccpCheckSession.id)
            .where(HaccpCleaningLog.session_id.in_(session_ids))
            .order_by(HaccpCleaningLog.completed_at)
        )).all()
        if session_ids else []
    )

    cleaning_logs = []
    for row in cleaning_q:
        c, stype = row
        task = cleaning_task_map.get(c.task_id)
        cleaning_logs.append({
            "completed_at": _fmt_datetime(c.completed_at),
            "session_label": "Ouverture" if stype == "opening" else "Fermeture",
            "task_name": task.name if task else f"#{c.task_id}",
            "zone": task.zone if task else "—",
            "is_compliant": c.is_compliant,
            "notes": c.notes,
        })

    # Non-conformités
    nc_q = (await db.execute(
        select(HaccpNonConformity).where(
            and_(
                HaccpNonConformity.created_at >= datetime.combine(from_date, datetime.min.time()),
                HaccpNonConformity.created_at <= datetime.combine(to_date, datetime.max.time()),
            )
        ).order_by(HaccpNonConformity.created_at)
    )).scalars().all()

    non_conformities = [
        {
            "created_at": _fmt_datetime(nc.created_at),
            "source_label": _SOURCE_LABELS.get(nc.source_type, nc.source_type),
            "description": nc.description,
            "status": nc.status,
            "corrective_action": nc.corrective_action,
            "validated_at": _fmt_datetime(nc.validated_at),
        }
        for nc in nc_q
    ]

    # Réceptions
    reception_q = (await db.execute(
        select(HaccpReceptionControl).where(
            and_(
                HaccpReceptionControl.delivery_date >= from_date,
                HaccpReceptionControl.delivery_date <= to_date,
            )
        ).order_by(HaccpReceptionControl.delivery_date)
    )).scalars().all()

    reception_controls = [
        {
            "delivery_date": _fmt_date(r.delivery_date),
            "supplier_name": r.supplier_name,
            "temperature_on_arrival": f"{r.temperature_on_arrival:.1f}" if r.temperature_on_arrival is not None else None,
            "packaging_ok": r.packaging_ok,
            "labeling_ok": r.labeling_ok,
            "dlc_ok": r.dlc_ok,
            "is_accepted": r.is_accepted,
            "refusal_reason": r.refusal_reason,
        }
        for r in reception_q
    ]

    # Refroidissements
    cooling_q = (await db.execute(
        select(HaccpCoolingLog).where(
            and_(
                HaccpCoolingLog.started_at >= datetime.combine(from_date, datetime.min.time()),
                HaccpCoolingLog.started_at <= datetime.combine(to_date, datetime.max.time()),
            )
        ).order_by(HaccpCoolingLog.started_at)
    )).scalars().all()

    cooling_logs = []
    for cl in cooling_q:
        duration = None
        if cl.ended_at and cl.started_at:
            duration = int((cl.ended_at - cl.started_at).total_seconds() / 60)
        cooling_logs.append({
            "started_at": _fmt_datetime(cl.started_at),
            "product_name": cl.product_name,
            "temp_start": f"{cl.temp_start:.1f}",
            "temp_final": f"{cl.temp_final:.1f}" if cl.temp_final is not None else None,
            "duration_minutes": duration,
            "is_compliant": cl.is_compliant,
            "corrective_action": cl.corrective_action,
        })

    # Formations
    training_q = (await db.execute(
        select(HaccpTrainingRecord).order_by(HaccpTrainingRecord.training_date.desc())
    )).scalars().all()

    today = date.today()
    training_records = []
    for tr in training_q:
        expires_soon = (
            tr.expiry_date is not None
            and not tr.expiry_date < today
            and (tr.expiry_date - today).days <= 30
        )
        training_records.append({
            "training_type_label": _TRAINING_TYPE_LABELS.get(tr.training_type, tr.training_type),
            "training_date": _fmt_date(tr.training_date),
            "expiry_date": _fmt_date(tr.expiry_date) if tr.expiry_date else None,
            "provider": tr.provider,
            "certificate_ref": tr.certificate_ref,
            "is_expired": tr.expiry_date is not None and tr.expiry_date < today,
            "expires_soon": expires_soon,
        })

    total_days = (to_date - from_date).days + 1

    return {
        "restaurant_name": restaurant_name,
        "from_date": _fmt_date(from_date),
        "to_date": _fmt_date(to_date),
        "generated_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "total_days": total_days,
        "sessions": sessions,
        "temperature_logs": temperature_logs,
        "dlc_checks": dlc_checks,
        "cleaning_logs": cleaning_logs,
        "non_conformities": non_conformities,
        "reception_controls": reception_controls,
        "cooling_logs": cooling_logs,
        "training_records": training_records,
    }


# ── Export PDF ────────────────────────────────────────────────────────────────

def _render_pdf_sync(context: dict) -> bytes:
    """Génère le PDF de manière synchrone (WeasyPrint est bloquant).

    [⚠️ PROD] À appeler via run_in_executor pour ne pas bloquer l'event loop.
    """
    from weasyprint import HTML  # import local car WeasyPrint charge Cairo au startup

    template = _jinja_env.get_template("export_pdf.html")
    html_content = template.render(**context)
    return HTML(string=html_content).write_pdf()


async def generate_pdf(
    db: AsyncSession,
    from_date: date,
    to_date: date,
    restaurant_name: str,
) -> bytes:
    """Collecte les données et génère le PDF HACCP.

    Exécuté dans un thread pool pour ne pas bloquer l'event loop.
    """
    context = await _collect_data(db, from_date, to_date, restaurant_name)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _render_pdf_sync, context)


# ── Export CSV ────────────────────────────────────────────────────────────────

async def generate_csv(
    db: AsyncSession,
    from_date: date,
    to_date: date,
    restaurant_name: str,
    data_type: str = "all",
) -> tuple[str, bytes]:
    """Génère le CSV HACCP pour le type demandé.

    Returns:
        Tuple[filename, csv_bytes]
    """
    context = await _collect_data(db, from_date, to_date, restaurant_name)
    period = f"{from_date.strftime('%Y%m%d')}_{to_date.strftime('%Y%m%d')}"
    output = io.StringIO()

    if data_type == "temperatures" or data_type == "all":
        writer = csv.writer(output)
        if data_type == "all":
            output.write(f"# RELEVÉS DE TEMPÉRATURE — {restaurant_name} — {period}\n")
        writer.writerow(["Date/Heure", "Session", "Équipement", "T° mesurée (°C)", "Plage cible", "Conforme", "Action corrective"])
        for t in context["temperature_logs"]:
            writer.writerow([
                t["logged_at"], t["session_label"], t["equipment_name"],
                t["measured_temp"], t["target_range"],
                "Oui" if t["is_compliant"] else "Non",
                t["corrective_action"] or "",
            ])
        output.write("\n")

    if data_type == "dlc" or data_type == "all":
        writer = csv.writer(output)
        if data_type == "all":
            output.write(f"# VÉRIFICATIONS DLC — {restaurant_name} — {period}\n")
        writer.writerow(["Date/Heure", "Ingrédient", "Niveau DLC", "Date DLC", "Conforme", "Action corrective"])
        for d in context["dlc_checks"]:
            writer.writerow([
                d["logged_at"], d["ingredient_name"], d["level_label"],
                d["dlc_date"], "Oui" if d["is_compliant"] else "Non",
                d["corrective_action"] or "",
            ])
        output.write("\n")

    if data_type == "nc" or data_type == "all":
        writer = csv.writer(output)
        if data_type == "all":
            output.write(f"# NON-CONFORMITÉS — {restaurant_name} — {period}\n")
        writer.writerow(["Date", "Source", "Description", "Statut", "Action corrective", "Clôturée le"])
        for nc in context["non_conformities"]:
            writer.writerow([
                nc["created_at"], nc["source_label"], nc["description"],
                nc["status"], nc["corrective_action"] or "", nc["validated_at"],
            ])
        output.write("\n")

    if data_type == "reception" or data_type == "all":
        writer = csv.writer(output)
        if data_type == "all":
            output.write(f"# CONTRÔLES RÉCEPTION — {restaurant_name} — {period}\n")
        writer.writerow(["Date livraison", "Fournisseur", "T° réception (°C)", "Emballage OK", "Étiquetage OK", "DLC OK", "Accepté", "Motif refus"])
        for r in context["reception_controls"]:
            writer.writerow([
                r["delivery_date"], r["supplier_name"],
                r["temperature_on_arrival"] or "",
                "Oui" if r["packaging_ok"] else "Non",
                "Oui" if r["labeling_ok"] else "Non",
                "Oui" if r["dlc_ok"] else "Non",
                "Oui" if r["is_accepted"] else "Non",
                r["refusal_reason"] or "",
            ])
        output.write("\n")

    if data_type == "cooling" or data_type == "all":
        writer = csv.writer(output)
        if data_type == "all":
            output.write(f"# REFROIDISSEMENT RAPIDE — {restaurant_name} — {period}\n")
        writer.writerow(["Date début", "Produit", "T° initiale (°C)", "T° finale (°C)", "Durée (min)", "Conforme", "Action corrective"])
        for cl in context["cooling_logs"]:
            writer.writerow([
                cl["started_at"], cl["product_name"],
                cl["temp_start"], cl["temp_final"] or "En cours",
                cl["duration_minutes"] or "", "Oui" if cl["is_compliant"] else "Non",
                cl["corrective_action"] or "",
            ])

    filename = f"haccp_{period}_{data_type}.csv"
    csv_bytes = output.getvalue().encode("utf-8-sig")  # utf-8-sig pour Excel FR
    return filename, csv_bytes
