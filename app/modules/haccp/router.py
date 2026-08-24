"""Router FastAPI — module HACCP / sécurité alimentaire.

Préfixe : /haccp (enregistré dans main.py)

Architecture des routes :
- GET  /haccp/status/today         -- gate bloquant (ouverture/fermeture)
- POST /haccp/sessions             -- démarrer une session ouverture|fermeture
- GET  /haccp/sessions/today       -- sessions du jour
- PATCH /haccp/sessions/{id}/complete -- valider une session
- CRUD /haccp/equipment            -- équipements (admin)
- POST /haccp/sessions/{id}/temperatures -- relevé température
- POST /haccp/sessions/{id}/dlc    -- vérification DLC
- CRUD /haccp/cleaning-tasks       -- plan ND (admin)
- POST /haccp/sessions/{id}/cleaning   -- réalisation tâche ND
- GET/PATCH /haccp/non-conformities    -- NC + actions correctives
- POST /haccp/reception-controls   -- contrôle à réception
- POST/PATCH /haccp/cooling        -- refroidissement rapide
- POST /haccp/training             -- formation hygiène staff
- POST /haccp/sessions/{id}/oil    -- huiles de friture (optionnel)

[🔒 SÉCURITÉ]
- Toutes les routes nécessitent un JWT tenant valide.
- Les routes admin (création équipements, tâches ND, formation) nécessitent role=admin.
- Les routes de completion de session nécessitent role=admin (manager).
"""

from datetime import date

from fastapi import APIRouter, Depends

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.modules.haccp import service
from app.modules.haccp.schemas import (
    HaccpCleaningLogCreate,
    HaccpCleaningLogResponse,
    HaccpCleaningTaskCreate,
    HaccpCleaningTaskResponse,
    HaccpCleaningTaskUpdate,
    HaccpCoolingCreate,
    HaccpCoolingResponse,
    HaccpCoolingUpdate,
    HaccpDlcCheckCreate,
    HaccpDlcCheckResponse,
    HaccpEquipmentCreate,
    HaccpEquipmentResponse,
    HaccpEquipmentUpdate,
    HaccpFryingOilCreate,
    HaccpFryingOilResponse,
    HaccpNonConformityCreate,
    HaccpNonConformityResponse,
    HaccpNonConformityUpdate,
    HaccpReceptionCreate,
    HaccpReceptionResponse,
    HaccpSessionComplete,
    HaccpSessionCreate,
    HaccpSessionResponse,
    HaccpStatusResponse,
    HaccpTemperatureCreate,
    HaccpTemperatureResponse,
    HaccpTrainingCreate,
    HaccpTrainingResponse,
)

router = APIRouter()


# ─── Status (gate bloquant) ───────────────────────────────────────────────────

@router.get(
    "/status/today",
    response_model=HaccpStatusResponse,
    summary="État HACCP du jour (gate bloquant ouverture/fermeture)",
)
async def get_today_status(
    current_user: dict = Depends(get_current_user),
) -> HaccpStatusResponse:
    """GET /haccp/status/today

    Retourne l'état complet des sessions du jour : avancement des checks,
    can_open (ouverture possible), can_close (fermeture possible).

    Utilisé par l'app Flutter pour afficher la progression et débloquer
    les boutons d'ouverture/fermeture du restaurant.
    """
    today = date.today()
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_haccp_status(session, today)


# ─── Sessions ─────────────────────────────────────────────────────────────────

@router.post(
    "/sessions",
    response_model=HaccpSessionResponse,
    status_code=201,
    summary="Démarrer une session HACCP (ouverture ou fermeture)",
)
async def create_session(
    body: HaccpSessionCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpSessionResponse:
    """POST /haccp/sessions

    Crée ou retourne la session du jour pour le type donné.
    Si une session existe déjà pour (date, session_type), elle est retournée
    sans erreur (idempotent).
    """
    check_date = body.date or date.today()
    user_id = int(current_user["id"])

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        check_session = await service.get_or_create_session(
            session,
            session_type=body.session_type,
            check_date=check_date,
            user_id=user_id,
        )
        return HaccpSessionResponse.model_validate(check_session)


@router.get(
    "/sessions/today",
    response_model=list[HaccpSessionResponse],
    summary="Sessions HACCP du jour",
)
async def get_today_sessions(
    current_user: dict = Depends(get_current_user),
) -> list[HaccpSessionResponse]:
    """GET /haccp/sessions/today — retourne les sessions en cours aujourd'hui."""
    from sqlalchemy import select, and_
    from app.modules.haccp.models import HaccpCheckSession

    today = date.today()
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        stmt = select(HaccpCheckSession).where(HaccpCheckSession.date == today)
        result = await session.execute(stmt)
        sessions = result.scalars().all()
        return [HaccpSessionResponse.model_validate(s) for s in sessions]


@router.patch(
    "/sessions/{session_id}/complete",
    response_model=HaccpSessionResponse,
    summary="Valider une session HACCP (débloque le gate ouverture/fermeture)",
)
async def complete_session(
    session_id: int,
    body: HaccpSessionComplete,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpSessionResponse:
    """PATCH /haccp/sessions/{id}/complete

    [🔒 SÉCURITÉ] Réservé aux managers (role=admin).

    Marque la session comme complète. Si des éléments sont manquants et
    que ``force=false`` (défaut), retourne 422 avec la liste des manquants.
    Si ``force=true``, valide avec statut ``incomplete_validated``.

    Une session ``complete`` ou ``incomplete_validated`` débloque le gate :
    - opening → le restaurant peut ouvrir
    - closing → la fermeture peut être confirmée
    """
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        check_session = await service.complete_session(
            session,
            session_id=session_id,
            user_id=user_id,
            notes=body.notes,
            force=body.force,
        )
        return HaccpSessionResponse.model_validate(check_session)


# ─── Equipment ────────────────────────────────────────────────────────────────

@router.get(
    "/equipment",
    response_model=list[HaccpEquipmentResponse],
    summary="Lister les équipements HACCP",
)
async def list_equipment(
    active_only: bool = True,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpEquipmentResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        equipment = await service.list_equipment(session, active_only=active_only)
        return [HaccpEquipmentResponse.model_validate(e) for e in equipment]


@router.post(
    "/equipment",
    response_model=HaccpEquipmentResponse,
    status_code=201,
    summary="Créer un équipement HACCP (admin)",
)
async def create_equipment(
    body: HaccpEquipmentCreate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpEquipmentResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        equipment = await service.create_equipment(session, body.model_dump())
        return HaccpEquipmentResponse.model_validate(equipment)


@router.patch(
    "/equipment/{equipment_id}",
    response_model=HaccpEquipmentResponse,
    summary="Modifier un équipement HACCP (admin)",
)
async def update_equipment(
    equipment_id: int,
    body: HaccpEquipmentUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpEquipmentResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        equipment = await service.update_equipment(
            session,
            equipment_id,
            body.model_dump(exclude_none=True),
        )
        return HaccpEquipmentResponse.model_validate(equipment)


# ─── Temperature Logs ─────────────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/temperatures",
    response_model=HaccpTemperatureResponse,
    status_code=201,
    summary="Enregistrer un relevé de température",
)
async def log_temperature(
    session_id: int,
    body: HaccpTemperatureCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpTemperatureResponse:
    """POST /haccp/sessions/{id}/temperatures

    Enregistre la température mesurée pour un équipement.
    La conformité est calculée automatiquement vs les bornes de l'équipement.
    Si hors limite, une non-conformité est créée automatiquement.
    """
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        log = await service.log_temperature(session, session_id, body.model_dump(), user_id)
        return HaccpTemperatureResponse.model_validate(log)


@router.get(
    "/sessions/{session_id}/temperatures",
    response_model=list[HaccpTemperatureResponse],
    summary="Relevés de température d'une session",
)
async def list_temperatures(
    session_id: int,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpTemperatureResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        logs = await service.list_temperature_logs(session, session_id)
        return [HaccpTemperatureResponse.model_validate(l) for l in logs]


# ─── DLC Checks ───────────────────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/dlc",
    response_model=HaccpDlcCheckResponse,
    status_code=201,
    summary="Enregistrer une vérification DLC",
)
async def log_dlc(
    session_id: int,
    body: HaccpDlcCheckCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpDlcCheckResponse:
    """POST /haccp/sessions/{id}/dlc

    Enregistre une vérification DLC (niveau 1, 2, ou 3).
    Si ``is_compliant=false``, une non-conformité est créée automatiquement.
    """
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        check = await service.log_dlc_check(session, session_id, body.model_dump(), user_id)
        return HaccpDlcCheckResponse.model_validate(check)


@router.get(
    "/sessions/{session_id}/dlc",
    response_model=list[HaccpDlcCheckResponse],
    summary="Vérifications DLC d'une session",
)
async def list_dlc(
    session_id: int,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpDlcCheckResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        checks = await service.list_dlc_checks(session, session_id)
        return [HaccpDlcCheckResponse.model_validate(c) for c in checks]


# ─── Cleaning Tasks ───────────────────────────────────────────────────────────

@router.get(
    "/cleaning-tasks",
    response_model=list[HaccpCleaningTaskResponse],
    summary="Lister les tâches du plan ND",
)
async def list_cleaning_tasks(
    session_type: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpCleaningTaskResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        tasks = await service.list_cleaning_tasks(session, session_type=session_type)
        return [HaccpCleaningTaskResponse.model_validate(t) for t in tasks]


@router.post(
    "/cleaning-tasks",
    response_model=HaccpCleaningTaskResponse,
    status_code=201,
    summary="Créer une tâche ND (admin)",
)
async def create_cleaning_task(
    body: HaccpCleaningTaskCreate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpCleaningTaskResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        task = await service.create_cleaning_task(session, body.model_dump())
        return HaccpCleaningTaskResponse.model_validate(task)


@router.patch(
    "/cleaning-tasks/{task_id}",
    response_model=HaccpCleaningTaskResponse,
    summary="Modifier une tâche ND (admin)",
)
async def update_cleaning_task(
    task_id: int,
    body: HaccpCleaningTaskUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpCleaningTaskResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        task = await service.update_cleaning_task(
            session, task_id, body.model_dump(exclude_none=True)
        )
        return HaccpCleaningTaskResponse.model_validate(task)


@router.post(
    "/sessions/{session_id}/cleaning",
    response_model=HaccpCleaningLogResponse,
    status_code=201,
    summary="Marquer une tâche ND comme réalisée",
)
async def log_cleaning(
    session_id: int,
    body: HaccpCleaningLogCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpCleaningLogResponse:
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        log = await service.log_cleaning(session, session_id, body.model_dump(), user_id)
        return HaccpCleaningLogResponse.model_validate(log)


@router.get(
    "/sessions/{session_id}/cleaning",
    response_model=list[HaccpCleaningLogResponse],
    summary="Tâches ND réalisées dans une session",
)
async def list_cleaning_logs(
    session_id: int,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpCleaningLogResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        logs = await service.list_cleaning_logs(session, session_id)
        return [HaccpCleaningLogResponse.model_validate(l) for l in logs]


# ─── Non-Conformities ─────────────────────────────────────────────────────────

@router.get(
    "/non-conformities",
    response_model=list[HaccpNonConformityResponse],
    summary="Lister les non-conformités",
)
async def list_non_conformities(
    status: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpNonConformityResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        ncs = await service.list_non_conformities(session, status=status)
        return [HaccpNonConformityResponse.model_validate(nc) for nc in ncs]


@router.post(
    "/non-conformities",
    response_model=HaccpNonConformityResponse,
    status_code=201,
    summary="Créer une non-conformité manuelle",
)
async def create_non_conformity(
    body: HaccpNonConformityCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpNonConformityResponse:
    from app.modules.haccp.models import HaccpNonConformity

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        nc = HaccpNonConformity(**body.model_dump())
        session.add(nc)
        await session.commit()
        await session.refresh(nc)
        return HaccpNonConformityResponse.model_validate(nc)


@router.patch(
    "/non-conformities/{nc_id}",
    response_model=HaccpNonConformityResponse,
    summary="Ajouter action corrective / valider une NC (admin)",
)
async def update_non_conformity(
    nc_id: int,
    body: HaccpNonConformityUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpNonConformityResponse:
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        nc = await service.update_non_conformity(
            session, nc_id, body.model_dump(exclude_none=True), user_id
        )
        return HaccpNonConformityResponse.model_validate(nc)


# ─── Reception Controls ───────────────────────────────────────────────────────

@router.post(
    "/reception-controls",
    response_model=HaccpReceptionResponse,
    status_code=201,
    summary="Enregistrer un contrôle à réception",
)
async def create_reception(
    body: HaccpReceptionCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpReceptionResponse:
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        control = await service.create_reception_control(session, body.model_dump(), user_id)
        return HaccpReceptionResponse.model_validate(control)


@router.get(
    "/reception-controls",
    response_model=list[HaccpReceptionResponse],
    summary="Historique des contrôles à réception",
)
async def list_reception(
    current_user: dict = Depends(get_current_user),
) -> list[HaccpReceptionResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        controls = await service.list_reception_controls(session)
        return [HaccpReceptionResponse.model_validate(c) for c in controls]


# ─── Cooling Logs ─────────────────────────────────────────────────────────────

@router.post(
    "/cooling",
    response_model=HaccpCoolingResponse,
    status_code=201,
    summary="Démarrer un suivi de refroidissement rapide",
)
async def create_cooling(
    body: HaccpCoolingCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpCoolingResponse:
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        log = await service.create_cooling_log(session, body.model_dump(), user_id)
        return HaccpCoolingResponse.model_validate(log)


@router.patch(
    "/cooling/{log_id}",
    response_model=HaccpCoolingResponse,
    summary="Mettre à jour un suivi de refroidissement (relevés intermédiaires)",
)
async def update_cooling(
    log_id: int,
    body: HaccpCoolingUpdate,
    current_user: dict = Depends(get_current_user),
) -> HaccpCoolingResponse:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        log = await service.update_cooling_log(
            session, log_id, body.model_dump(exclude_none=True)
        )
        return HaccpCoolingResponse.model_validate(log)


@router.get(
    "/cooling",
    response_model=list[HaccpCoolingResponse],
    summary="Suivis de refroidissement (actifs ou récents)",
)
async def list_cooling(
    active_only: bool = False,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpCoolingResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        logs = await service.list_cooling_logs(session, active_only=active_only)
        return [HaccpCoolingResponse.model_validate(l) for l in logs]


# ─── Training Records ─────────────────────────────────────────────────────────

@router.post(
    "/training",
    response_model=HaccpTrainingResponse,
    status_code=201,
    summary="Enregistrer une formation hygiène (admin)",
)
async def create_training(
    body: HaccpTrainingCreate,
    current_user: dict = Depends(require_role("admin")),
) -> HaccpTrainingResponse:
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        record = await service.create_training_record(session, body.model_dump(), user_id)
        return HaccpTrainingResponse.model_validate(record)


@router.get(
    "/training",
    response_model=list[HaccpTrainingResponse],
    summary="Historique des formations hygiène",
)
async def list_training(
    user_id: int | None = None,
    current_user: dict = Depends(get_current_user),
) -> list[HaccpTrainingResponse]:
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        records = await service.list_training_records(session, user_id=user_id)
        return [HaccpTrainingResponse.model_validate(r) for r in records]


# ─── Frying Oil Logs ─────────────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/oil",
    response_model=HaccpFryingOilResponse,
    status_code=201,
    summary="Enregistrer un contrôle huile de friture (optionnel)",
)
async def log_oil(
    session_id: int,
    body: HaccpFryingOilCreate,
    current_user: dict = Depends(get_current_user),
) -> HaccpFryingOilResponse:
    """POST /haccp/sessions/{id}/oil

    Optionnel — uniquement pour les tenants avec friteuses.
    Seuil légal polarity : < 25% AGL.
    Si non-conforme, une non-conformité est créée automatiquement.
    """
    user_id = int(current_user["id"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        log = await service.log_frying_oil(session, session_id, body.model_dump(), user_id)
        return HaccpFryingOilResponse.model_validate(log)
