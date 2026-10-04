"""Read-only counts for phase-1 rollout. No validation or activation side effects."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.haccp.foundation_contracts import EstablishmentReadiness


async def inspect_foundations(session: AsyncSession) -> EstablishmentReadiness:
    sites = (
        (await session.execute(text("SELECT id FROM establishments ORDER BY id")))
        .scalars()
        .all()
    )
    queries = {
        "establishments": "SELECT count(*) FROM establishments",
        "unassigned_equipment": "SELECT count(*) FROM haccp_equipment e WHERE NOT EXISTS (SELECT 1 FROM haccp_equipment_scopes s WHERE s.equipment_id=e.id)",
        "unassigned_cleaning_tasks": "SELECT count(*) FROM haccp_cleaning_tasks t WHERE NOT EXISTS (SELECT 1 FROM haccp_cleaning_scopes s WHERE s.task_id=t.id)",
        "unassigned_batches": "SELECT count(*) FROM ingredient_batches b WHERE NOT EXISTS (SELECT 1 FROM stock_batch_positions p WHERE p.batch_id=b.id)",
        "ingredients_without_site_stock": "SELECT count(*) FROM ingredients i WHERE NOT EXISTS (SELECT 1 FROM establishment_stock s WHERE s.ingredient_id=i.id)",
        "unreconciled_stock": "SELECT count(*) FROM establishment_stock WHERE reconciled_at IS NULL",
        "unknown_batch_remainders": "SELECT count(*) FROM stock_batch_positions WHERE remaining_quantity IS NULL",
        "legacy_sessions": "SELECT count(*) FROM haccp_check_sessions",
        "strict_sessions": "SELECT count(*) FROM haccp_daily_sessions",
        "active_sites_without_hours": "SELECT count(*) FROM establishments e WHERE e.is_active AND NOT EXISTS (SELECT 1 FROM establishment_business_hours h WHERE h.establishment_id=e.id AND h.is_active)",
        "unresolved_cleaning_schedule": "SELECT count(*) FROM haccp_cleaning_tasks t JOIN haccp_cleaning_scopes s ON s.task_id=t.id WHERE t.is_active AND (t.frequency='per_service' OR (t.frequency IN ('weekly','monthly') AND s.due_from IS NULL))",
        "equipment_without_limits": "SELECT count(*) FROM haccp_equipment WHERE is_active AND target_min_temp IS NULL AND target_max_temp IS NULL",
        "legacy_stock_without_batch": "SELECT count(*) FROM ingredients i WHERE current_qty > 0 AND NOT EXISTS (SELECT 1 FROM ingredient_batches b WHERE b.ingredient_id=i.id AND b.status NOT IN ('consumed','discarded'))",
        "legacy_batches_without_dlc": "SELECT count(*) FROM ingredient_batches WHERE expires_at IS NULL AND status NOT IN ('consumed','discarded')",
        "ambiguous_legacy_hour_slots": "SELECT count(*) FROM (SELECT day_of_week, slot_index FROM business_hours GROUP BY day_of_week,slot_index HAVING count(*) > 1) slots",
    }
    counts = {
        key: int(await session.scalar(text(sql)) or 0) for key, sql in queries.items()
    }
    blockers = [
        key
        for key, count in counts.items()
        if count and key not in {"establishments", "legacy_sessions", "strict_sessions"}
    ]
    if len(sites) != 1:
        blockers.append("explicit_establishment_assignment_required")
    blockers.append("phase1_only_activation_unavailable")
    return EstablishmentReadiness(
        establishment_id=sites[0] if len(sites) == 1 else None,
        reference_date=date.today(),
        strict_enabled=bool(
            await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM establishment_operations WHERE strict_enabled)"
                )
            )
        ),
        counts=counts,
        blockers=blockers,
    )
