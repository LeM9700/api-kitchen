"""Service HACCP — logique métier.

Séparation stricte :
- router.py  : validation HTTP, dépendances FastAPI, réponses
- service.py : logique métier, requêtes DB, règles HACCP
- models.py  : ORM uniquement
"""

from datetime import date, datetime, timezone

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.haccp.models import (
    HaccpCheckSession,
    HaccpCleaningLog,
    HaccpCleaningTask,
    HaccpCoolingLog,
    HaccpDlcCheck,
    HaccpEquipment,
    HaccpFryingOilLog,
    HaccpNonConformity,
    HaccpReceptionControl,
    HaccpTemperatureLog,
    HaccpTrainingRecord,
)
from app.modules.haccp.schemas import (
    HaccpSessionSummary,
    HaccpStatusResponse,
)

# ─── Constantes HACCP ────────────────────────────────────────────────────────

# Seuil légal huile de friture (% AGL par polarité)
OIL_POLARITY_LIMIT = 25.0

# Températures max refroidissement rapide (arrêté 21/12/2009)
COOLING_TARGET_TEMP = 10.0  # °C atteint en 2h max


# ─── Equipment ───────────────────────────────────────────────────────────────

async def list_equipment(session: AsyncSession, active_only: bool = True) -> list[HaccpEquipment]:
    """Retourne les équipements du tenant.

    Args:
        session: Session DB tenant.
        active_only: Si True, ne retourne que les équipements actifs.

    Returns:
        Liste d'équipements triés par nom.
    """
    stmt = select(HaccpEquipment)
    if active_only:
        stmt = stmt.where(HaccpEquipment.is_active.is_(True))
    stmt = stmt.order_by(HaccpEquipment.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_equipment(session: AsyncSession, equipment_id: int) -> HaccpEquipment:
    """Récupère un équipement par ID.

    Raises:
        AppError: NOT_FOUND si introuvable.
    """
    equipment = await session.get(HaccpEquipment, equipment_id)
    if not equipment:
        raise AppError("NOT_FOUND", "Équipement introuvable.", 404)
    return equipment


async def create_equipment(session: AsyncSession, data: dict) -> HaccpEquipment:
    """Crée un équipement HACCP.

    Args:
        session: Session DB tenant.
        data: Champs validés depuis HaccpEquipmentCreate.

    Returns:
        Équipement créé.
    """
    equipment = HaccpEquipment(**data)
    session.add(equipment)
    await session.commit()
    await session.refresh(equipment)
    return equipment


async def update_equipment(session: AsyncSession, equipment_id: int, data: dict) -> HaccpEquipment:
    """Met à jour un équipement (champs fournis uniquement).

    Raises:
        AppError: NOT_FOUND si introuvable.
    """
    equipment = await get_equipment(session, equipment_id)
    for key, value in data.items():
        if value is not None:
            setattr(equipment, key, value)
    await session.commit()
    await session.refresh(equipment)
    return equipment


# ─── Check Sessions ───────────────────────────────────────────────────────────

async def get_or_create_session(
    session: AsyncSession,
    session_type: str,
    check_date: date,
    user_id: int,
) -> HaccpCheckSession:
    """Récupère la session du jour ou en crée une nouvelle.

    [⚠️ PROD] Contrainte UNIQUE (date, session_type) — une seule session
    par type par jour. Si elle existe déjà, on la retourne sans erreur.

    Args:
        session: Session DB tenant.
        session_type: "opening" ou "closing".
        check_date: Date de la session (défaut = today).
        user_id: ID de l'utilisateur qui démarre la session.

    Returns:
        Session existante ou nouvellement créée.
    """
    stmt = select(HaccpCheckSession).where(
        and_(
            HaccpCheckSession.date == check_date,
            HaccpCheckSession.session_type == session_type,
        )
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        return existing

    check_session = HaccpCheckSession(
        session_type=session_type,
        date=check_date,
        started_by=user_id,
        status="in_progress",
    )
    session.add(check_session)
    await session.commit()
    await session.refresh(check_session)
    return check_session


async def get_session_by_id(session: AsyncSession, session_id: int) -> HaccpCheckSession:
    """Récupère une session par ID.

    Raises:
        AppError: NOT_FOUND si introuvable.
    """
    check_session = await session.get(HaccpCheckSession, session_id)
    if not check_session:
        raise AppError("NOT_FOUND", "Session HACCP introuvable.", 404)
    return check_session


async def complete_session(
    session: AsyncSession,
    session_id: int,
    user_id: int,
    notes: str | None,
    force: bool,
) -> HaccpCheckSession:
    """Marque une session comme complète.

    Si ``force=True``, utilise le statut ``incomplete_validated`` pour
    permettre la validation même avec des éléments manquants (décision manager).

    [⚠️ PROD] Un session ``complete`` ou ``incomplete_validated`` déverrouille
    le gate ouverture/fermeture côté app client.

    Args:
        session: Session DB tenant.
        session_id: ID de la session à compléter.
        user_id: ID du manager qui valide.
        notes: Notes optionnelles.
        force: Si True → incomplete_validated même si des checks manquent.

    Returns:
        Session mise à jour.

    Raises:
        AppError: CONFLICT si déjà complétée.
    """
    check_session = await get_session_by_id(session, session_id)

    if check_session.status in ("complete", "incomplete_validated"):
        raise AppError("CONFLICT", "Cette session est déjà complétée.", 409)

    # Vérification des éléments requis
    equipment_count = await _count_equipment_for_session(session, check_session.session_type)
    temp_count = await _count_temperature_logs(session, session_id)
    cleaning_count = await _count_cleaning_tasks_for_session(session, check_session.session_type)
    cleaning_done = await _count_cleaning_logs(session, session_id)

    all_done = (temp_count >= equipment_count) and (cleaning_done >= cleaning_count)

    if not all_done and not force:
        missing = []
        if temp_count < equipment_count:
            missing.append(f"{equipment_count - temp_count} relevé(s) de température manquant(s)")
        if cleaning_done < cleaning_count:
            missing.append(f"{cleaning_count - cleaning_done} tâche(s) ND manquante(s)")
        raise AppError(
            "INCOMPLETE_SESSION",
            f"Session incomplète : {', '.join(missing)}. Utilisez force=true pour valider quand même.",
            422,
        )

    new_status = "complete" if all_done else "incomplete_validated"
    check_session.status = new_status
    check_session.completed_by = user_id
    check_session.completed_at = datetime.now(timezone.utc)
    check_session.notes = notes

    await session.commit()
    await session.refresh(check_session)
    return check_session


async def _count_equipment_for_session(session: AsyncSession, session_type: str) -> int:
    """Nombre d'équipements actifs qui doivent être contrôlés pour ce type de session."""
    if session_type == "opening":
        stmt = select(func.count()).select_from(HaccpEquipment).where(
            and_(HaccpEquipment.is_active.is_(True), HaccpEquipment.check_at_opening.is_(True))
        )
    else:
        stmt = select(func.count()).select_from(HaccpEquipment).where(
            and_(HaccpEquipment.is_active.is_(True), HaccpEquipment.check_at_closing.is_(True))
        )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _count_temperature_logs(session: AsyncSession, session_id: int) -> int:
    stmt = select(func.count()).select_from(HaccpTemperatureLog).where(
        HaccpTemperatureLog.session_id == session_id
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _count_cleaning_tasks_for_session(session: AsyncSession, session_type: str) -> int:
    stmt = select(func.count()).select_from(HaccpCleaningTask).where(
        and_(
            HaccpCleaningTask.is_active.is_(True),
            HaccpCleaningTask.session_type.in_([session_type, "both"]),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _count_cleaning_logs(session: AsyncSession, session_id: int) -> int:
    stmt = select(func.count()).select_from(HaccpCleaningLog).where(
        HaccpCleaningLog.session_id == session_id
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


# ─── Temperature Logs ─────────────────────────────────────────────────────────

async def log_temperature(
    session: AsyncSession,
    session_id: int,
    data: dict,
    user_id: int,
) -> HaccpTemperatureLog:
    """Enregistre un relevé de température et calcule la conformité.

    La conformité est calculée en comparant ``measured_temp`` aux bornes
    de l'équipement. Si l'équipement n'a pas de bornes définies, on marque
    ``is_compliant=True`` (pas de limite définie = pas de non-conformité auto).

    [⚠️ PROD] Si non-conforme, une HaccpNonConformity est créée automatiquement.

    Args:
        session: Session DB tenant.
        session_id: ID de la session en cours.
        data: Champs depuis HaccpTemperatureCreate.
        user_id: ID de l'utilisateur qui saisit.

    Returns:
        Relevé créé.

    Raises:
        AppError: NOT_FOUND si session ou équipement introuvable.
    """
    check_session = await get_session_by_id(session, session_id)
    equipment = await get_equipment(session, data["equipment_id"])

    # Calcul conformité
    is_compliant = True
    if equipment.target_min_temp is not None and equipment.target_max_temp is not None:
        is_compliant = equipment.target_min_temp <= data["measured_temp"] <= equipment.target_max_temp

    log = HaccpTemperatureLog(
        session_id=session_id,
        equipment_id=data["equipment_id"],
        measured_temp=data["measured_temp"],
        is_compliant=is_compliant,
        corrective_action=data.get("corrective_action"),
        logged_by=user_id,
    )
    session.add(log)
    await session.flush()

    # Non-conformité automatique si hors limite
    if not is_compliant:
        nc = HaccpNonConformity(
            session_id=session_id,
            source_type="temperature",
            source_id=log.id,
            description=(
                f"{equipment.name} : {data['measured_temp']}°C "
                f"(limite : {equipment.target_min_temp}°C – {equipment.target_max_temp}°C)"
            ),
            status="open",
        )
        session.add(nc)

    await session.commit()
    await session.refresh(log)
    return log


async def list_temperature_logs(session: AsyncSession, session_id: int) -> list[HaccpTemperatureLog]:
    stmt = select(HaccpTemperatureLog).where(
        HaccpTemperatureLog.session_id == session_id
    ).order_by(HaccpTemperatureLog.logged_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── DLC Checks ───────────────────────────────────────────────────────────────

async def log_dlc_check(
    session: AsyncSession,
    session_id: int,
    data: dict,
    user_id: int,
) -> HaccpDlcCheck:
    """Enregistre une vérification DLC.

    Si ``is_compliant=False``, crée une non-conformité automatiquement.

    Args:
        session: Session DB tenant.
        session_id: ID de la session.
        data: Champs depuis HaccpDlcCheckCreate.
        user_id: ID de l'utilisateur.

    Returns:
        Vérification DLC créée.
    """
    await get_session_by_id(session, session_id)

    check = HaccpDlcCheck(
        session_id=session_id,
        **{k: v for k, v in data.items()},
        logged_by=user_id,
    )
    session.add(check)
    await session.flush()

    if not data.get("is_compliant", True):
        nc = HaccpNonConformity(
            session_id=session_id,
            source_type="dlc",
            source_id=check.id,
            description=(
                f"DLC niveau {data['dlc_level']} dépassée : "
                f"{data['ingredient_name']} — {data['dlc_date']}"
                + (f" ({data['location']})" if data.get("location") else "")
            ),
            status="open",
        )
        session.add(nc)

    await session.commit()
    await session.refresh(check)
    return check


async def list_dlc_checks(session: AsyncSession, session_id: int) -> list[HaccpDlcCheck]:
    stmt = select(HaccpDlcCheck).where(
        HaccpDlcCheck.session_id == session_id
    ).order_by(HaccpDlcCheck.logged_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Cleaning Tasks ───────────────────────────────────────────────────────────

async def list_cleaning_tasks(
    session: AsyncSession,
    session_type: str | None = None,
    active_only: bool = True,
) -> list[HaccpCleaningTask]:
    """Retourne les tâches ND filtrées par type de session si fourni."""
    stmt = select(HaccpCleaningTask)
    conditions = []
    if active_only:
        conditions.append(HaccpCleaningTask.is_active.is_(True))
    if session_type:
        conditions.append(HaccpCleaningTask.session_type.in_([session_type, "both"]))
    if conditions:
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(HaccpCleaningTask.zone, HaccpCleaningTask.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_cleaning_task(session: AsyncSession, data: dict) -> HaccpCleaningTask:
    task = HaccpCleaningTask(**data)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def update_cleaning_task(session: AsyncSession, task_id: int, data: dict) -> HaccpCleaningTask:
    task = await session.get(HaccpCleaningTask, task_id)
    if not task:
        raise AppError("NOT_FOUND", "Tâche ND introuvable.", 404)
    for key, value in data.items():
        if value is not None:
            setattr(task, key, value)
    await session.commit()
    await session.refresh(task)
    return task


async def log_cleaning(
    session: AsyncSession,
    session_id: int,
    data: dict,
    user_id: int,
) -> HaccpCleaningLog:
    """Marque une tâche ND comme réalisée dans une session.

    Raises:
        AppError: CONFLICT si déjà réalisée dans cette session.
    """
    await get_session_by_id(session, session_id)

    # Vérifie si déjà fait
    existing = await session.execute(
        select(HaccpCleaningLog).where(
            and_(
                HaccpCleaningLog.session_id == session_id,
                HaccpCleaningLog.task_id == data["task_id"],
            )
        )
    )
    if existing.scalar_one_or_none():
        raise AppError("CONFLICT", "Cette tâche est déjà marquée comme réalisée.", 409)

    log = HaccpCleaningLog(
        session_id=session_id,
        task_id=data["task_id"],
        completed_by=user_id,
        notes=data.get("notes"),
        is_compliant=data.get("is_compliant", True),
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


async def list_cleaning_logs(session: AsyncSession, session_id: int) -> list[HaccpCleaningLog]:
    stmt = select(HaccpCleaningLog).where(
        HaccpCleaningLog.session_id == session_id
    ).order_by(HaccpCleaningLog.completed_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Non-Conformities ─────────────────────────────────────────────────────────

async def list_non_conformities(
    session: AsyncSession,
    status: str | None = None,
) -> list[HaccpNonConformity]:
    stmt = select(HaccpNonConformity)
    if status:
        stmt = stmt.where(HaccpNonConformity.status == status)
    stmt = stmt.order_by(HaccpNonConformity.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_non_conformity(
    session: AsyncSession,
    nc_id: int,
    data: dict,
    user_id: int,
) -> HaccpNonConformity:
    """Met à jour une non-conformité (action corrective + statut).

    Si le statut passe à ``closed``, enregistre le validateur.

    Args:
        session: Session DB tenant.
        nc_id: ID de la non-conformité.
        data: Champs depuis HaccpNonConformityUpdate.
        user_id: ID du manager qui valide.

    Returns:
        Non-conformité mise à jour.

    Raises:
        AppError: NOT_FOUND si introuvable.
    """
    nc = await session.get(HaccpNonConformity, nc_id)
    if not nc:
        raise AppError("NOT_FOUND", "Non-conformité introuvable.", 404)

    if data.get("corrective_action"):
        nc.corrective_action = data["corrective_action"]

    if data.get("status"):
        nc.status = data["status"]
        if data["status"] == "closed":
            nc.validated_by = user_id
            nc.validated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(nc)
    return nc


# ─── Reception Controls ───────────────────────────────────────────────────────

async def create_reception_control(
    session: AsyncSession,
    data: dict,
    user_id: int,
) -> HaccpReceptionControl:
    control = HaccpReceptionControl(**data, logged_by=user_id)
    session.add(control)
    await session.commit()
    await session.refresh(control)
    return control


async def list_reception_controls(
    session: AsyncSession,
    limit: int = 50,
) -> list[HaccpReceptionControl]:
    stmt = (
        select(HaccpReceptionControl)
        .order_by(HaccpReceptionControl.logged_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Cooling Logs ─────────────────────────────────────────────────────────────

async def create_cooling_log(
    session: AsyncSession,
    data: dict,
    user_id: int,
) -> HaccpCoolingLog:
    log = HaccpCoolingLog(**data, logged_by=user_id)
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


async def update_cooling_log(
    session: AsyncSession,
    log_id: int,
    data: dict,
) -> HaccpCoolingLog:
    """Met à jour un log de refroidissement (relevés intermédiaires + final).

    Calcule ``is_compliant`` automatiquement si ``temp_final`` est fourni.
    Crée une non-conformité si le refroidissement n'est pas atteint en temps voulu.
    """
    log = await session.get(HaccpCoolingLog, log_id)
    if not log:
        raise AppError("NOT_FOUND", "Log de refroidissement introuvable.", 404)

    for key, value in data.items():
        if value is not None:
            setattr(log, key, value)

    # Calcul conformité : temp_final doit être ≤ COOLING_TARGET_TEMP
    if log.temp_final is not None:
        log.is_compliant = log.temp_final <= COOLING_TARGET_TEMP

        if not log.is_compliant:
            nc = HaccpNonConformity(
                source_type="cooling",
                source_id=log.id,
                description=(
                    f"Refroidissement non conforme : {log.product_name} "
                    f"— {log.temp_final}°C (objectif ≤{COOLING_TARGET_TEMP}°C)"
                ),
                status="open",
            )
            session.add(nc)

    await session.commit()
    await session.refresh(log)
    return log


async def list_cooling_logs(session: AsyncSession, active_only: bool = False) -> list[HaccpCoolingLog]:
    stmt = select(HaccpCoolingLog)
    if active_only:
        stmt = stmt.where(HaccpCoolingLog.ended_at.is_(None))
    stmt = stmt.order_by(HaccpCoolingLog.started_at.desc()).limit(50)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Training Records ─────────────────────────────────────────────────────────

async def create_training_record(
    session: AsyncSession,
    data: dict,
    user_id: int,
) -> HaccpTrainingRecord:
    record = HaccpTrainingRecord(**data, logged_by=user_id)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def list_training_records(
    session: AsyncSession,
    user_id: int | None = None,
) -> list[HaccpTrainingRecord]:
    stmt = select(HaccpTrainingRecord)
    if user_id:
        stmt = stmt.where(HaccpTrainingRecord.user_id == user_id)
    stmt = stmt.order_by(HaccpTrainingRecord.training_date.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─── Frying Oil Logs ─────────────────────────────────────────────────────────

async def log_frying_oil(
    session: AsyncSession,
    session_id: int,
    data: dict,
    user_id: int,
) -> HaccpFryingOilLog:
    """Enregistre un contrôle huile de friture.

    Conformité : polarity < 25% ET couleur OK ET odeur OK.
    Si non-conforme, crée une non-conformité automatiquement.
    """
    await get_session_by_id(session, session_id)

    polarity_ok = (
        data.get("polarity_percent") is None
        or data["polarity_percent"] < OIL_POLARITY_LIMIT
    )
    # data vient de body.model_dump() : color_ok/odor_ok sont toujours presentes
    # (None si non renseignees) -- dict.get(cle, defaut) n'applique le defaut
    # que si la cle est absente, jamais si elle vaut deja None explicitement.
    is_compliant = polarity_ok and data.get("color_ok") is not False and data.get("odor_ok") is not False

    log = HaccpFryingOilLog(
        session_id=session_id,
        is_compliant=is_compliant,
        logged_by=user_id,
        **{k: v for k, v in data.items()},
    )
    session.add(log)
    await session.flush()

    if not is_compliant:
        reasons = []
        if not polarity_ok:
            reasons.append(f"polarité {data['polarity_percent']}% ≥ {OIL_POLARITY_LIMIT}%")
        if not data.get("color_ok", True):
            reasons.append("couleur non conforme")
        if not data.get("odor_ok", True):
            reasons.append("odeur non conforme")

        nc = HaccpNonConformity(
            session_id=session_id,
            source_type="other",
            source_id=log.id,
            description=f"Huile {data['fryer_name']} non conforme : {', '.join(reasons)}",
            status="open",
        )
        session.add(nc)

    await session.commit()
    await session.refresh(log)
    return log


# ─── Status (gate bloquant) ───────────────────────────────────────────────────

async def get_haccp_status(session: AsyncSession, today: date) -> HaccpStatusResponse:
    """Retourne l'état HACCP du jour (gate bloquant ouverture/fermeture).

    Utilisé par l'app Flutter pour déterminer si le restaurant peut ouvrir
    (``can_open``) ou si la fermeture peut être confirmée (``can_close``).

    Args:
        session: Session DB tenant.
        today: Date du jour.

    Returns:
        HaccpStatusResponse avec can_open, can_close et résumés des sessions.
    """

    async def _build_summary(stype: str) -> HaccpSessionSummary:
        stmt = select(HaccpCheckSession).where(
            and_(
                HaccpCheckSession.date == today,
                HaccpCheckSession.session_type == stype,
            )
        )
        result = await session.execute(stmt)
        cs = result.scalar_one_or_none()

        if not cs:
            equip_total = await _count_equipment_for_session(session, stype)
            cleaning_total = await _count_cleaning_tasks_for_session(session, stype)
            return HaccpSessionSummary(
                session_id=None,
                status="not_started",
                temperatures_done=0,
                temperatures_total=equip_total,
                dlc_done=0,
                cleaning_done=0,
                cleaning_total=cleaning_total,
                has_non_conformities=False,
            )

        temps_done = await _count_temperature_logs(session, cs.id)
        temps_total = await _count_equipment_for_session(session, stype)

        dlc_stmt = select(func.count()).select_from(HaccpDlcCheck).where(
            HaccpDlcCheck.session_id == cs.id
        )
        dlc_done = (await session.execute(dlc_stmt)).scalar_one() or 0

        cleaning_done = await _count_cleaning_logs(session, cs.id)
        cleaning_total = await _count_cleaning_tasks_for_session(session, stype)

        nc_stmt = select(func.count()).select_from(HaccpNonConformity).where(
            and_(
                HaccpNonConformity.session_id == cs.id,
                HaccpNonConformity.status != "closed",
            )
        )
        has_nc = ((await session.execute(nc_stmt)).scalar_one() or 0) > 0

        return HaccpSessionSummary(
            session_id=cs.id,
            status=cs.status,
            temperatures_done=temps_done,
            temperatures_total=temps_total,
            dlc_done=dlc_done,
            cleaning_done=cleaning_done,
            cleaning_total=cleaning_total,
            has_non_conformities=has_nc,
        )

    opening_summary = await _build_summary("opening")
    closing_summary = await _build_summary("closing")

    # Compter les NC ouvertes toutes sessions confondues
    open_nc_stmt = select(func.count()).select_from(HaccpNonConformity).where(
        HaccpNonConformity.status != "closed"
    )
    open_nc = (await session.execute(open_nc_stmt)).scalar_one() or 0

    can_open = opening_summary.status in ("complete", "incomplete_validated")
    can_close = closing_summary.status in ("complete", "incomplete_validated")

    return HaccpStatusResponse(
        today=today,
        opening=opening_summary,
        closing=closing_summary,
        can_open=can_open,
        can_close=can_close,
        open_non_conformities=open_nc,
    )
