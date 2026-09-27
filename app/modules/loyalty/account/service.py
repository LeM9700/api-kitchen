import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.security import get_password_hash
from app.core.config import settings
from app.core.http.errors import AppError
from app.core.sms.service import enqueue_sms
from app.modules.auth.models import User
from app.modules.customer.service import normalize_phone_e164
from app.modules.loyalty.account.models import LoyaltyAccount, LoyaltyPointReservation, LoyaltyTransaction
from app.modules.loyalty.account.schemas import (
    ExpiringPointsResponse,
    LoyaltyQrTokenResponse,
    LoyaltyStaffCustomerCreateRequest,
    LoyaltyStaffCustomerOut,
    LoyaltyStaffCustomerSearchResponse,
    LoyaltyStaffCustomerWalletOut,
    LoyaltyTransactionPage,
)

_STAFF_PHONE_SEARCH_MIN_DIGITS = 4


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _phone_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _mask_phone(phone_e164: str | None) -> str | None:
    digits = _phone_digits(phone_e164)
    if len(digits) < 4:
        return None
    prefix = phone_e164[:3] if phone_e164 and phone_e164.startswith("+") else "+"
    return f"{prefix}••••••{digits[-4:]}"


def _qr_secret() -> bytes:
    secret = settings.loyalty_qr_secret or settings.jwt_secret
    return secret.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign_qr_payload(payload_part: str) -> str:
    digest = hmac.new(_qr_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _encode_qr_token(payload: dict) -> str:
    payload_part = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_part}.{_sign_qr_payload(payload_part)}"


def _decode_qr_token(token: str) -> dict:
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError as exc:
        raise AppError("INVALID_LOYALTY_QR", "QR fidelite invalide", 401, "token") from exc

    expected = _sign_qr_payload(payload_part)
    if not hmac.compare_digest(signature, expected):
        raise AppError("INVALID_LOYALTY_QR", "QR fidelite invalide", 401, "token")

    try:
        payload = json.loads(_b64url_decode(payload_part))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AppError("INVALID_LOYALTY_QR", "QR fidelite invalide", 401, "token") from exc

    exp = int(payload.get("exp") or 0)
    if exp < int(datetime.now(timezone.utc).timestamp()):
        raise AppError("LOYALTY_QR_EXPIRED", "QR fidelite expire", 401, "token")
    return payload


async def _points_by_user(session: AsyncSession, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not user_ids:
        return {}

    account_rows = await session.execute(
        select(LoyaltyAccount.user_id, LoyaltyAccount.points).where(LoyaltyAccount.user_id.in_(user_ids))
    )
    points = {int(row.user_id): int(row.points or 0) for row in account_rows}

    now = datetime.now(timezone.utc)
    reserved_rows = await session.execute(
        select(
            LoyaltyPointReservation.user_id,
            func.coalesce(func.sum(LoyaltyPointReservation.points_reserved), 0),
        )
        .where(
            LoyaltyPointReservation.user_id.in_(user_ids),
            LoyaltyPointReservation.status == "reserved",
            LoyaltyPointReservation.expires_at > now,
        )
        .group_by(LoyaltyPointReservation.user_id)
    )
    reserved = {int(row[0]): int(row[1] or 0) for row in reserved_rows}
    return {
        user_id: (
            points.get(user_id, 0),
            max(0, points.get(user_id, 0) - reserved.get(user_id, 0)),
        )
        for user_id in user_ids
    }


def _build_staff_customer_out(user: User, points: int = 0, available_points: int | None = None) -> LoyaltyStaffCustomerOut:
    phone_e164 = getattr(user, "phone_e164", None)
    return LoyaltyStaffCustomerOut(
        id=user.id,
        full_name=user.full_name,
        masked_phone=_mask_phone(phone_e164),
        phone_last4=_phone_digits(phone_e164)[-4:] or None,
        points=points,
        available_points=points if available_points is None else available_points,
        phone_verified=user.phone_verified_at is not None,
        pending_profile_completion=bool(getattr(user, "pending_profile_completion", False)),
    )


async def _get_staff_customer(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None or user.role != "customer" or not user.is_active:
        raise AppError("CUSTOMER_NOT_FOUND", "Client introuvable", 404, "customer_id")
    return user


async def _record_admin_audit(session: AsyncSession, **kwargs) -> None:
    from app.modules.admin.customers.service import record_admin_audit

    await record_admin_audit(session, **kwargs)


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


async def search_staff_customers(
    session: AsyncSession,
    query: str,
    *,
    actor: dict,
    limit: int = 10,
) -> LoyaltyStaffCustomerSearchResponse:
    digits = _phone_digits(query)
    if len(digits) < _STAFF_PHONE_SEARCH_MIN_DIGITS:
        await _record_admin_audit(
            session,
            actor=actor,
            action="loyalty_staff_search_too_short",
            target_type="customer",
            target_id="phone_lookup",
            metadata={"digits_count": len(digits), "min_digits": _STAFF_PHONE_SEARCH_MIN_DIGITS},
        )
        await session.commit()
        return LoyaltyStaffCustomerSearchResponse(items=[], min_digits=_STAFF_PHONE_SEARCH_MIN_DIGITS)

    pattern = f"%{digits}%"
    result = await session.execute(
        select(User)
        .where(
            User.role == "customer",
            User.is_active.is_(True),
            User.phone_verified_at.is_not(None),
            User.phone_e164.is_not(None),
            or_(
                func.regexp_replace(User.phone_e164, r"\D", "", "g").like(pattern),
                func.regexp_replace(User.phone, r"\D", "", "g").like(pattern),
            ),
        )
        .order_by(User.full_name.asc().nulls_last(), User.id.asc())
        .limit(limit)
    )
    users = list(result.scalars())
    balances = await _points_by_user(session, [user.id for user in users])
    await _record_admin_audit(
        session,
        actor=actor,
        action="loyalty_staff_phone_search",
        target_type="customer",
        target_id="phone_lookup",
        metadata={"digits_count": len(digits), "result_count": len(users)},
    )
    await session.commit()
    return LoyaltyStaffCustomerSearchResponse(
        items=[
            _build_staff_customer_out(user, points=balances.get(user.id, (0, 0))[0], available_points=balances.get(user.id, (0, 0))[1])
            for user in users
        ],
        min_digits=_STAFF_PHONE_SEARCH_MIN_DIGITS,
    )


async def create_staff_customer(
    session: AsyncSession,
    body: LoyaltyStaffCustomerCreateRequest,
    *,
    actor: dict,
    tenant_slug: str,
    arq_pool=None,
) -> LoyaltyStaffCustomerWalletOut:
    from app.modules.loyalty.config.service import list_reward_catalog_with_eligibility

    phone_e164 = normalize_phone_e164(body.phone)
    user = await session.scalar(select(User).where(User.phone_e164 == phone_e164))
    created = False
    if user is not None and user.role != "customer":
        raise AppError("PHONE_ALREADY_USED", "Ce telephone est deja utilise par un autre compte", 409, "phone")
    if user is not None and not user.is_active:
        raise AppError("ACCOUNT_DISABLED", "Ce compte client est desactive", 403, "phone")
    if user is None:
        user = User(
            email=None,
            password_hash=get_password_hash(secrets.token_urlsafe(32)),
            full_name=body.full_name,
            phone=body.phone,
            phone_e164=phone_e164,
            role="customer",
            pending_profile_completion=True,
        )
        session.add(user)
        await session.flush()
        created = True
    else:
        user.full_name = user.full_name or body.full_name
        user.phone = user.phone or body.phone

    account = await get_or_create_account(session, user.id, commit=False)
    account_points = int(account.points or 0)
    await _record_admin_audit(
        session,
        actor=actor,
        action="loyalty_staff_customer_created" if created else "loyalty_staff_customer_reused",
        target_type="customer",
        target_id=user.id,
        metadata={
            "phone_last4": _phone_digits(phone_e164)[-4:],
            "pending_profile_completion": bool(getattr(user, "pending_profile_completion", False)),
        },
    )
    await session.commit()
    await session.refresh(user)

    link = settings.client_app_download_url or settings.app_base_url
    if "?" in link:
        app_link = f"{link}&{urlencode({'tenant': tenant_slug})}"
    else:
        app_link = f"{link}?{urlencode({'tenant': tenant_slug})}"
    await enqueue_sms(
        arq_pool,
        to_phone_e164=phone_e164,
        body=f"Votre compte fidelite est pret. Telechargez l'application et finalisez votre inscription: {app_link}",
    )
    await _record_admin_audit(
        session,
        actor=actor,
        action="loyalty_staff_signup_sms_sent",
        target_type="customer",
        target_id=user.id,
        metadata={
            "phone_last4": _phone_digits(phone_e164)[-4:],
            "tenant": tenant_slug,
        },
    )
    await session.commit()

    rewards = await list_reward_catalog_with_eligibility(session, account_points)
    return LoyaltyStaffCustomerWalletOut(
        customer=_build_staff_customer_out(user, points=account_points, available_points=account_points),
        rewards=rewards,
    )


async def build_staff_customer_wallet(
    session: AsyncSession,
    customer_id: int,
    *,
    actor: dict,
) -> LoyaltyStaffCustomerWalletOut:
    from app.modules.loyalty.config.service import list_reward_catalog_with_eligibility

    user = await _get_staff_customer(session, customer_id)
    balances = await _points_by_user(session, [customer_id])
    points, available_points = balances.get(customer_id, (0, 0))
    await _record_admin_audit(
        session,
        actor=actor,
        action="loyalty_staff_wallet_viewed",
        target_type="customer",
        target_id=customer_id,
        metadata={"available_points": available_points},
    )
    await session.commit()
    return LoyaltyStaffCustomerWalletOut(
        customer=_build_staff_customer_out(user, points=points, available_points=available_points),
        rewards=await list_reward_catalog_with_eligibility(session, available_points),
    )


async def create_loyalty_qr_token(
    session: AsyncSession,
    *,
    user_id: int,
    tenant_slug: str,
) -> LoyaltyQrTokenResponse:
    user = await _get_staff_customer(session, user_id)
    if user.phone_verified_at is None:
        raise AppError("PHONE_NOT_VERIFIED", "Telephone client non verifie", 403, "phone")

    ttl_seconds = int(settings.loyalty_qr_ttl_seconds)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    token = _encode_qr_token(
        {
            "typ": "loyalty_qr",
            "tenant": tenant_slug,
            "sub": user.id,
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(16),
        }
    )
    await _record_admin_audit(
        session,
        actor={"id": user.id, "email": user.email},
        action="loyalty_qr_generated",
        target_type="customer",
        target_id=user.id,
        metadata={"ttl_seconds": ttl_seconds},
    )
    await session.commit()
    return LoyaltyQrTokenResponse(token=token, expires_at=expires_at, ttl_seconds=ttl_seconds)


async def identify_loyalty_qr_token(
    session: AsyncSession,
    token: str,
    *,
    tenant_slug: str,
    actor: dict,
) -> LoyaltyStaffCustomerWalletOut:
    payload = _decode_qr_token(token)
    if payload.get("typ") != "loyalty_qr" or payload.get("tenant") != tenant_slug:
        raise AppError("INVALID_LOYALTY_QR", "QR fidelite invalide", 401, "token")

    try:
        customer_id = int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_LOYALTY_QR", "QR fidelite invalide", 401, "token") from exc

    user = await _get_staff_customer(session, customer_id)
    if user.phone_verified_at is None:
        raise AppError("PHONE_NOT_VERIFIED", "Telephone client non verifie", 403, "phone")

    wallet = await build_staff_customer_wallet(session, customer_id, actor=actor)
    await _record_admin_audit(
        session,
        actor=actor,
        action="loyalty_staff_qr_identified",
        target_type="customer",
        target_id=customer_id,
        metadata={"token_ttl_remaining_seconds": max(0, int(payload.get("exp", 0)) - int(datetime.now(timezone.utc).timestamp()))},
    )
    await session.commit()
    return wallet


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
