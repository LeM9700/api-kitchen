from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.modules.loyalty.account.models import LoyaltyAccount, LoyaltyPointReservation, LoyaltyTransaction
from app.modules.loyalty.account.schemas import ExpiringPointsBucket, ExpiringPointsResponse, LoyaltyTransactionPage


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def credit_points(
    session: AsyncSession,
    tenant_slug: str,
    user_id: int,
    order_total: float,
) -> LoyaltyAccount:
    points = int(order_total)
    return await add_points(
        session,
        user_id,
        points,
        reason=f"order_delivered_{tenant_slug}",
        transaction_type="earn",
        source="order",
    )


async def get_or_create_account(session: AsyncSession, user_id: int, *, commit: bool = True) -> LoyaltyAccount:
    account = await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id))
    if account is None:
        account = LoyaltyAccount(user_id=user_id, points=0)
        session.add(account)
        if commit:
            await session.commit()
            await session.refresh(account)
        else:
            await session.flush()
    return account


async def build_account_response(session: AsyncSession, user_id: int):
    from app.modules.loyalty.config.service import get_or_create_loyalty_config, get_expiring_points
    from app.modules.loyalty.account.schemas import LoyaltyAccountOut

    account = await get_or_create_account(session, user_id, commit=False)
    config = await get_or_create_loyalty_config(session)
    expiring = await get_expiring_points(session, user_id)
    await session.commit()
    return LoyaltyAccountOut(
        id=account.id,
        user_id=account.user_id,
        points=account.points,
        point_value_euros=Decimal(account.points) * Decimal(str(config.points_to_euro_rate)),
        expiring_soon_points=expiring.total_expiring_points,
    )


async def add_points(
    session: AsyncSession,
    user_id: int,
    points: int,
    reason: str,
    *,
    changed_by_user_id: int | None = None,
    transaction_type: str = "manual",
    source: str = "admin",
    order_id: int | None = None,
    reward_id: int | None = None,
    reservation_id: int | None = None,
    metadata: dict | None = None,
) -> LoyaltyAccount:
    if points <= 0:
        raise AppError("INVALID_POINTS", "points must be greater than zero", 422, "points")

    account = await get_or_create_account(session, user_id, commit=False)
    account.points += points
    session.add(
        LoyaltyTransaction(
            account_id=account.id,
            points_delta=points,
            reason=reason,
            transaction_type=transaction_type,
            source=source,
            changed_by_user_id=changed_by_user_id,
            order_id=order_id,
            reward_id=reward_id,
            reservation_id=reservation_id,
            metadata_json=metadata,
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


async def redeem_points(
    session: AsyncSession,
    user_id: int,
    points: int,
    reason: str = "redeem",
    *,
    source: str = "checkout",
    order_id: int | None = None,
    reward_id: int | None = None,
    reservation_id: int | None = None,
    metadata: dict | None = None,
) -> LoyaltyAccount:
    if points <= 0:
        raise AppError("INVALID_POINTS", "points must be greater than zero", 422, "points")

    await get_or_create_account(session, user_id, commit=False)
    result = await session.execute(
        update(LoyaltyAccount)
        .where(LoyaltyAccount.user_id == user_id, LoyaltyAccount.points >= points)
        .values(points=LoyaltyAccount.points - points)
        .returning(LoyaltyAccount.id, LoyaltyAccount.points)
    )
    row = result.first()
    if row is None:
        raise AppError("INSUFFICIENT_POINTS", "Solde de points insuffisant", 422, "points")

    session.add(
        LoyaltyTransaction(
            account_id=row.id,
            points_delta=-points,
            reason=reason,
            transaction_type="redeem",
            source=source,
            order_id=order_id,
            reward_id=reward_id,
            reservation_id=reservation_id,
            metadata_json=metadata,
        )
    )
    await session.commit()
    return await get_or_create_account(session, user_id)


async def list_transactions(
    session: AsyncSession,
    user_id: int,
    *,
    page: int = 1,
    limit: int = 20,
    transaction_type: str | None = None,
) -> LoyaltyTransactionPage:
    account = await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id))
    if account is None:
        return LoyaltyTransactionPage(items=[], page=page, limit=limit, total=0)

    filters = [LoyaltyTransaction.account_id == account.id]
    if transaction_type:
        filters.append(LoyaltyTransaction.transaction_type == transaction_type)

    total = await session.scalar(select(func.count()).select_from(LoyaltyTransaction).where(*filters))
    result = await session.execute(
        select(LoyaltyTransaction)
        .where(*filters)
        .order_by(LoyaltyTransaction.created_at.desc(), LoyaltyTransaction.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    return LoyaltyTransactionPage(items=list(result.scalars()), page=page, limit=limit, total=int(total or 0))


async def get_available_points(session: AsyncSession, user_id: int) -> int:
    account = await get_or_create_account(session, user_id, commit=False)
    now = datetime.now(timezone.utc)
    reserved = await session.scalar(
        select(func.coalesce(func.sum(LoyaltyPointReservation.points_reserved), 0)).where(
            LoyaltyPointReservation.user_id == user_id,
            LoyaltyPointReservation.status == "reserved",
            LoyaltyPointReservation.expires_at > now,
        )
    )
    return max(0, account.points - int(reserved or 0))


async def create_checkout_reservation(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    points_to_use: int,
) -> LoyaltyPointReservation:
    if points_to_use <= 0:
        raise AppError("INVALID_POINTS", "points_to_use must be greater than zero", 422, "points_to_use")

    from app.modules.loyalty.config.service import get_or_create_loyalty_config
    from app.modules.orders.models import Order

    order = await session.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise AppError("ORDER_NOT_FOUND", "Commande introuvable", 404, "order_id")
    if order.status not in {"pending", "confirmed", "queued", "preparing"}:
        raise AppError("ORDER_NOT_ELIGIBLE", "Commande non eligible a une reservation de points", 422, "order_id")

    available_points = await get_available_points(session, user_id)
    if available_points < points_to_use:
        raise AppError("INSUFFICIENT_POINTS", "Solde de points disponible insuffisant", 422, "points_to_use")

    config = await get_or_create_loyalty_config(session)
    discount_amount = Decimal(points_to_use) * Decimal(str(config.points_to_euro_rate))
    reservation = LoyaltyPointReservation(
        user_id=user_id,
        order_id=order_id,
        points_reserved=points_to_use,
        discount_amount=discount_amount,
        status="reserved",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    session.add(reservation)
    await session.flush()
    account = await get_or_create_account(session, user_id, commit=False)
    session.add(
        LoyaltyTransaction(
            account_id=account.id,
            points_delta=0,
            reason=f"reservation_created_{reservation.id}",
            transaction_type="reservation",
            source="checkout",
            order_id=order_id,
            reservation_id=reservation.id,
            metadata_json={
                "points_reserved": points_to_use,
                "discount_amount": str(discount_amount),
                "points_to_euro_rate": str(config.points_to_euro_rate),
            },
        )
    )
    await session.commit()
    await session.refresh(reservation)
    return reservation


async def confirm_checkout_reservation(
    session: AsyncSession,
    user_id: int,
    reservation_id: int,
) -> LoyaltyPointReservation:
    result = await session.execute(
        select(LoyaltyPointReservation)
        .where(LoyaltyPointReservation.id == reservation_id, LoyaltyPointReservation.user_id == user_id)
        .with_for_update()
    )
    reservation = result.scalar_one_or_none()
    if reservation is None:
        raise AppError("RESERVATION_NOT_FOUND", "Reservation introuvable", 404)
    if reservation.status == "confirmed":
        return reservation
    if reservation.status != "reserved":
        raise AppError("RESERVATION_NOT_ACTIVE", "Reservation non active", 422)
    if _as_utc(reservation.expires_at) < datetime.now(timezone.utc):
        reservation.status = "expired"
        await session.commit()
        raise AppError("RESERVATION_EXPIRED", "Reservation expiree", 422)

    debit = await session.execute(
        update(LoyaltyAccount)
        .where(LoyaltyAccount.user_id == user_id, LoyaltyAccount.points >= reservation.points_reserved)
        .values(points=LoyaltyAccount.points - reservation.points_reserved)
        .returning(LoyaltyAccount.id)
    )
    row = debit.first()
    if row is None:
        raise AppError("INSUFFICIENT_POINTS", "Solde de points insuffisant", 422, "points")
    session.add(
        LoyaltyTransaction(
            account_id=row.id,
            points_delta=-reservation.points_reserved,
            reason=f"checkout_reservation_{reservation.id}",
            transaction_type="redeem",
            source="checkout",
            order_id=reservation.order_id,
            reservation_id=reservation.id,
            metadata_json={"discount_amount": str(reservation.discount_amount)},
        )
    )
    reservation.status = "confirmed"
    reservation.confirmed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(reservation)
    return reservation


async def cancel_checkout_reservation(
    session: AsyncSession,
    user_id: int,
    reservation_id: int,
) -> LoyaltyPointReservation:
    result = await session.execute(
        select(LoyaltyPointReservation)
        .where(LoyaltyPointReservation.id == reservation_id, LoyaltyPointReservation.user_id == user_id)
        .with_for_update()
    )
    reservation = result.scalar_one_or_none()
    if reservation is None:
        raise AppError("RESERVATION_NOT_FOUND", "Reservation introuvable", 404)
    if reservation.status in {"cancelled", "confirmed"}:
        return reservation
    reservation.status = "cancelled"
    reservation.cancelled_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(reservation)
    return reservation


async def get_expiring_points_response(session: AsyncSession, user_id: int) -> ExpiringPointsResponse:
    from app.modules.loyalty.config.service import get_expiring_points

    return await get_expiring_points(session, user_id)
