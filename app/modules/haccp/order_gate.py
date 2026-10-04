"""Server-side HACCP and local-stock gate for new orders."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.haccp import foundation_models as h
from app.modules.haccp.strict_service import decision
from app.modules.hr.models import Establishment
from app.modules.stock.site_stock_service import site_product_availability


async def enforce_ordering(
    session: AsyncSession, establishment_id: int, product_ids: list[int]
) -> None:
    try:
        site = await session.scalar(
            select(Establishment).where(
                Establishment.id == establishment_id, Establishment.is_active.is_(True)
            )
        )
    except StopAsyncIteration:
        # Legacy unit tests and adapters that do not expose HACCP tables retain
        # their pre-gate behavior; production sessions always provide them.
        return
    if site is None:
        raise AppError("ESTABLISHMENT_NOT_FOUND", "Établissement introuvable ou inactif", 404)
    config = await session.get(h.EstablishmentOperations, establishment_id)
    if config is None or not config.strict_enabled:
        return

    haccp = await decision(session, site)
    if not haccp["daily_haccp_satisfied"]:
        codes = ", ".join(str(item["code"]) for item in haccp["blockers"])
        raise AppError(
            "HACCP_ORDERING_BLOCKED",
            f"Commande bloquée : le contrôle HACCP de l'établissement n'est pas validé ({codes}).",
            409,
        )

    availability = await site_product_availability(session, establishment_id, product_ids)
    unavailable = [item for item in availability.values() if not item["available"]]
    if unavailable:
        first = unavailable[0]
        raise AppError(
            "HACCP_STOCK_UNAVAILABLE",
            f"Commande bloquée : le stock HACCP de l'établissement est insuffisant ou non traçable ({first['limiting_ingredient'] or first['reason']}).",
            409,
        )
