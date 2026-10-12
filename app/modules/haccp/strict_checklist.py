"""Read authoritative requirements. No counting shortcut, no writes or activation."""

import calendar
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.modules.haccp import foundation_models as h
from app.modules.haccp.foundation_contracts import HaccpBlockReason as R
from app.modules.haccp.models import HaccpCleaningTask, HaccpEquipment
from app.modules.stock.models import Ingredient, IngredientBatch
from app.modules.stock.site_models import EstablishmentStock, StockBatchPosition
from app.modules.stock.service import _effective_batch_expires_at


def iso(value):
    return value.isoformat() if value is not None else None


def cycle_start(day: date, anchor: date, frequency: str) -> date | None:
    """Latest anchored due date, including overdue work in its current period."""
    if day < anchor:
        return None
    if frequency == "weekly":
        from datetime import timedelta

        return anchor + timedelta(days=((day - anchor).days // 7) * 7)
    candidate = day.replace(
        day=min(anchor.day, calendar.monthrange(day.year, day.month)[1])
    )
    if candidate > day:
        year, month = (
            (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
        )
        candidate = date(
            year, month, min(anchor.day, calendar.monthrange(year, month)[1])
        )
    return candidate


async def build(session, site_id: int, day: date, kind: str, now: datetime):
    """Snapshot and live blockers for every required equipment/task/ingredient."""
    requirements, blockers = [], []
    equipment = (
        (
            await session.execute(
                select(HaccpEquipment)
                .join(
                    h.HaccpEquipmentScope,
                    h.HaccpEquipmentScope.equipment_id == HaccpEquipment.id,
                )
                .where(
                    h.HaccpEquipmentScope.establishment_id == site_id,
                    HaccpEquipment.is_active,
                )
                .order_by(HaccpEquipment.id)
                .with_for_update(of=HaccpEquipment)
            )
        )
        .scalars()
        .all()
    )
    tasks = (
        await session.execute(
            select(HaccpCleaningTask, h.HaccpCleaningScope)
            .join(
                h.HaccpCleaningScope,
                h.HaccpCleaningScope.task_id == HaccpCleaningTask.id,
            )
            .where(
                h.HaccpCleaningScope.establishment_id == site_id,
                HaccpCleaningTask.is_active,
            )
            .order_by(HaccpCleaningTask.id)
            .with_for_update()
        )
    ).all()
    for model, scope, column in (
        (HaccpEquipment, h.HaccpEquipmentScope, "equipment_id"),
        (HaccpCleaningTask, h.HaccpCleaningScope, "task_id"),
    ):
        unassigned = await session.scalar(
            select(model.id)
            .where(
                model.is_active,
                ~select(scope).where(getattr(scope, column) == model.id).exists(),
            )
            .limit(1)
        )
        if unassigned is not None:
            blockers.append(
                {
                    "code": R.NOT_CONFIGURED,
                    "subject": f"unassigned:{model.__tablename__}",
                }
            )
    if not equipment or not tasks:
        blockers.append(
            {"code": R.NOT_CONFIGURED, "subject": "equipment_or_cleaning_plan"}
        )
    for item in equipment:
        if not getattr(item, f"check_at_{kind}"):
            continue
        snapshot = {
            "name": item.name,
            "min": item.target_min_temp,
            "max": item.target_max_temp,
        }
        problems = []
        if (item.target_min_temp is None and item.target_max_temp is None) or (
            item.target_min_temp is not None
            and item.target_max_temp is not None
            and item.target_min_temp > item.target_max_temp
        ):
            problems.append(R.NOT_CONFIGURED)
        requirements.append(
            {
                "key": f"equipment:{item.id}",
                "kind": "temperature",
                "equipment_id": item.id,
                "snapshot": snapshot,
                "problems": problems,
            }
        )
    for task, scope in tasks:
        if task.session_type not in (kind, "both"):
            continue
        problems, due = [], day
        if task.frequency == "per_service" or (
            task.frequency in ("weekly", "monthly") and scope.due_from is None
        ):
            problems.append(R.NOT_CONFIGURED)
        elif task.frequency in ("weekly", "monthly"):
            due = cycle_start(day, scope.due_from, task.frequency)
            if due is None:
                continue
            # A completed strict session in this cycle is required; an isolated log is insufficient.
            done = await session.scalar(
                select(h.HaccpEvidence.id)
                .join(
                    h.HaccpRequirement,
                    h.HaccpRequirement.id == h.HaccpEvidence.requirement_id,
                )
                .join(
                    h.HaccpDailySession,
                    h.HaccpDailySession.id == h.HaccpRequirement.session_id,
                )
                .where(
                    h.HaccpRequirement.task_id == task.id,
                    h.HaccpDailySession.establishment_id == site_id,
                    h.HaccpDailySession.business_date >= due,
                    h.HaccpDailySession.business_date < day,
                    h.HaccpDailySession.status == "complete",
                    h.HaccpRequirement.session_revision == h.HaccpDailySession.revision,
                    h.HaccpEvidence.is_compliant,
                )
                .limit(1)
            )
            if done is not None:
                continue
        requirements.append(
            {
                "key": f"task:{task.id}",
                "kind": "cleaning",
                "task_id": task.id,
                "snapshot": {
                    "name": task.name,
                    "zone": task.zone,
                    "frequency": task.frequency,
                    "due": iso(due),
                    "role": task.required_role,
                    "product": task.product_used,
                },
                "problems": problems,
            }
        )
    stocks = (
        await session.execute(
            select(EstablishmentStock, Ingredient)
            .join(Ingredient)
            .where(EstablishmentStock.establishment_id == site_id)
            .order_by(Ingredient.id)
            .with_for_update()
        )
    ).all()
    unassigned = await session.scalar(
        select(Ingredient.id)
        .where(
            ~select(EstablishmentStock)
            .where(EstablishmentStock.ingredient_id == Ingredient.id)
            .exists()
        )
        .limit(1)
    )
    if unassigned is not None:
        blockers.append({"code": R.NOT_CONFIGURED, "subject": "unassigned_ingredients"})
    if not stocks:
        blockers.append({"code": R.NOT_CONFIGURED, "subject": "ingredient_inventory"})
    for stock, ingredient in stocks:
        positions = (
            await session.execute(
                select(StockBatchPosition, IngredientBatch)
                .join(
                    IngredientBatch, IngredientBatch.id == StockBatchPosition.batch_id
                )
                .where(
                    StockBatchPosition.establishment_id == site_id,
                    StockBatchPosition.ingredient_id == ingredient.id,
                )
                .order_by(IngredientBatch.id)
                .with_for_update()
            )
        ).all()
        lots, problems, total = [], [], Decimal(0)
        if stock.reconciled_at is None:
            problems.append(R.STOCK_UNRECONCILED)
        for position, batch in positions:
            expiry = _effective_batch_expires_at(batch)
            qty = position.remaining_quantity
            lots.append(
                {
                    "id": batch.id,
                    "revision": position.revision,
                    "remaining": str(qty) if qty is not None else None,
                    "status": batch.status,
                    "primary": iso(batch.expires_at),
                    "opened_at": iso(batch.opened_at),
                    "secondary_hours": batch.use_within_hours_after_opening,
                    "tertiary_at": iso(batch.tertiary_started_at),
                    "tertiary_hours": batch.tertiary_use_within_hours,
                    "effective_expires_at": iso(expiry),
                    "excluded_at": iso(position.excluded_at),
                    "excluded_by": position.excluded_by,
                    "exclusion_reason": position.exclusion_reason,
                }
            )
            if qty is None:
                problems.append(R.STOCK_UNRECONCILED)
                continue
            total += qty
            if qty == 0 or position.excluded_at is not None:
                continue
            if batch.status in ("consumed", "discarded"):
                problems.append(R.STOCK_UNRECONCILED)
            if (
                batch.expires_at is None
                or (
                    batch.status == "opened"
                    and (
                        batch.opened_at is None
                        or not batch.use_within_hours_after_opening
                    )
                )
                or (
                    batch.tertiary_started_at is not None
                    and not batch.tertiary_use_within_hours
                )
            ):
                problems.append(R.DLC_MISSING)
            if batch.status == "expired" or (expiry is not None and expiry <= now):
                problems.append(R.EXPIRED_BATCH_NOT_EXCLUDED)
        if total != stock.physical_quantity:
            problems.append(R.STOCK_UNRECONCILED)
        requirements.append(
            {
                "key": f"ingredient:{ingredient.id}",
                "kind": "dlc",
                "ingredient_id": ingredient.id,
                "snapshot": {
                    "name": ingredient.name,
                    "unit": ingredient.unit,
                    "physical_quantity": str(stock.physical_quantity),
                    "stock_revision": stock.revision,
                    "reconciled_at": iso(stock.reconciled_at),
                    "lots": lots,
                },
                "problems": list(dict.fromkeys(problems)),
            }
        )
    return requirements, blockers
