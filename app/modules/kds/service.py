import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http.errors import AppError
from app.modules.kds.models import KdsPairingCode, KdsRemoteSession, KdsScreen
from app.modules.kds.schemas import KdsScreenCreate, KdsScreenUpdate

PAIRING_CODE_TTL = timedelta(minutes=5)
# LOT 9 keeps remote sessions short-lived but usable for one service window.
REMOTE_SESSION_TTL = timedelta(hours=12)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    return _as_aware_utc(expires_at) <= now


def _hmac_digest(scope: str, tenant_slug: str, secret_value: str) -> str:
    hmac_secret = settings.jwt_hmac_secret or settings.jwt_secret
    message = f"{scope}:{tenant_slug}:{secret_value}".encode("utf-8")
    return hmac.new(hmac_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def hash_pairing_code(tenant_slug: str, code: str) -> str:
    return _hmac_digest("kds-pairing", tenant_slug, code)


def hash_remote_session_token(tenant_slug: str, token: str) -> str:
    return _hmac_digest("kds-session", tenant_slug, token)


def _generate_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def _generate_unique_pairing_code(
    session: AsyncSession,
    tenant_slug: str,
    now: datetime,
) -> tuple[str, str]:
    for _ in range(10):
        code = _generate_pairing_code()
        code_hash = hash_pairing_code(tenant_slug, code)
        existing = await session.scalar(
            select(KdsPairingCode.id).where(
                KdsPairingCode.code_hash == code_hash,
                KdsPairingCode.used_at.is_(None),
                KdsPairingCode.expires_at > now,
            )
        )
        if existing is None:
            return code, code_hash
    raise AppError("PAIRING_CODE_GENERATION_FAILED", "Unable to generate a pairing code", 503)


async def _generate_unique_remote_token(session: AsyncSession, tenant_slug: str) -> tuple[str, str]:
    for _ in range(10):
        token = secrets.token_urlsafe(32)
        token_hash = hash_remote_session_token(tenant_slug, token)
        existing = await session.scalar(
            select(KdsRemoteSession.id).where(KdsRemoteSession.session_token_hash == token_hash)
        )
        if existing is None:
            return token, token_hash
    raise AppError("KDS_SESSION_GENERATION_FAILED", "Unable to generate a remote session token", 503)


def _screen_payload(screen: KdsScreen) -> dict:
    return {
        "id": screen.id,
        "name": screen.name,
        "screen_key": screen.screen_key,
        "mode": screen.mode,
        "station": screen.station,
        "interaction_mode": screen.interaction_mode,
        "tickets_per_page": screen.tickets_per_page,
        "is_active": screen.is_active,
        "created_at": screen.created_at,
        "updated_at": screen.updated_at,
    }


def _remote_session_payload(remote: KdsRemoteSession, screen: KdsScreen) -> dict:
    return {
        "id": remote.id,
        "screen_id": remote.screen_id,
        "paired_by_user_id": remote.paired_by_user_id,
        "device_label": remote.device_label,
        "created_at": remote.created_at,
        "last_seen_at": remote.last_seen_at,
        "expires_at": remote.expires_at,
        "screen": _screen_payload(screen),
    }


async def _get_screen_or_404(session: AsyncSession, screen_id: int) -> KdsScreen:
    screen = await session.get(KdsScreen, screen_id)
    if screen is None:
        raise AppError("KDS_SCREEN_NOT_FOUND", "KDS screen not found", 404, "screen_id")
    return screen


async def list_screens(session: AsyncSession, *, include_inactive: bool = False) -> list[KdsScreen]:
    stmt = select(KdsScreen).order_by(KdsScreen.name, KdsScreen.id)
    if not include_inactive:
        stmt = stmt.where(KdsScreen.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_screen(session: AsyncSession, data: KdsScreenCreate) -> KdsScreen:
    existing = await session.scalar(select(KdsScreen.id).where(KdsScreen.screen_key == data.screen_key))
    if existing is not None:
        raise AppError("KDS_SCREEN_KEY_ALREADY_EXISTS", "screen_key already exists", 409, "screen_key")

    screen = KdsScreen(**data.model_dump())
    session.add(screen)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AppError("KDS_SCREEN_KEY_ALREADY_EXISTS", "screen_key already exists", 409, "screen_key") from exc
    await session.refresh(screen)
    return screen


async def update_screen(session: AsyncSession, screen_id: int, data: KdsScreenUpdate) -> KdsScreen:
    screen = await _get_screen_or_404(session, screen_id)
    updates = data.model_dump(exclude_unset=True)

    next_key = updates.get("screen_key")
    if next_key and next_key != screen.screen_key:
        existing = await session.scalar(select(KdsScreen.id).where(KdsScreen.screen_key == next_key))
        if existing is not None:
            raise AppError("KDS_SCREEN_KEY_ALREADY_EXISTS", "screen_key already exists", 409, "screen_key")

    for field, value in updates.items():
        setattr(screen, field, value)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if next_key:
            existing = await session.scalar(
                select(KdsScreen.id).where(KdsScreen.screen_key == next_key, KdsScreen.id != screen_id)
            )
            if existing is not None:
                raise AppError(
                    "KDS_SCREEN_KEY_ALREADY_EXISTS",
                    "screen_key already exists",
                    409,
                    "screen_key",
                ) from exc
        raise
    await session.refresh(screen)
    return screen


async def create_pairing_code(
    session: AsyncSession,
    *,
    tenant_slug: str,
    screen_id: int,
    created_by_user_id: int,
) -> dict:
    screen = await _get_screen_or_404(session, screen_id)
    if not screen.is_active:
        raise AppError("KDS_SCREEN_INACTIVE", "KDS screen is inactive", 409, "screen_id")

    now = _utcnow()
    await session.execute(
        update(KdsPairingCode)
        .where(
            KdsPairingCode.screen_id == screen_id,
            KdsPairingCode.used_at.is_(None),
            KdsPairingCode.expires_at > now,
        )
        .values(used_at=now)
    )
    code, code_hash = await _generate_unique_pairing_code(session, tenant_slug, now)
    pairing = KdsPairingCode(
        screen_id=screen_id,
        code_hash=code_hash,
        expires_at=now + PAIRING_CODE_TTL,
        created_by_user_id=created_by_user_id,
    )
    session.add(pairing)
    await session.commit()
    return {"screen_id": screen_id, "code": code, "expires_at": pairing.expires_at}


async def pair_remote(
    session: AsyncSession,
    *,
    tenant_slug: str,
    code: str,
    paired_by_user_id: int,
    device_label: str | None = None,
) -> dict:
    now = _utcnow()
    code_hash = hash_pairing_code(tenant_slug, code)
    result = await session.execute(
        select(KdsPairingCode)
        .where(KdsPairingCode.code_hash == code_hash)
        .order_by(KdsPairingCode.created_at.desc(), KdsPairingCode.id.desc())
        .with_for_update()
    )
    candidates = list(result.scalars().all())
    if not candidates:
        raise AppError("PAIRING_CODE_INVALID", "Invalid pairing code", 400, "code")

    pairing = candidates[0]
    if pairing.used_at is not None:
        raise AppError("PAIRING_CODE_ALREADY_USED", "Pairing code already used", 409, "code")
    if _is_expired(pairing.expires_at, now):
        raise AppError("PAIRING_CODE_EXPIRED", "Pairing code expired", 400, "code")

    screen = await _get_screen_or_404(session, pairing.screen_id)
    if not screen.is_active:
        raise AppError("KDS_SCREEN_INACTIVE", "KDS screen is inactive", 409, "screen_id")

    pairing.used_at = now
    token, token_hash = await _generate_unique_remote_token(session, tenant_slug)
    remote = KdsRemoteSession(
        session_token_hash=token_hash,
        screen_id=screen.id,
        paired_by_user_id=paired_by_user_id,
        device_label=device_label,
        created_at=now,
        last_seen_at=now,
        expires_at=now + REMOTE_SESSION_TTL,
    )
    session.add(remote)
    await session.commit()
    await session.refresh(remote)
    return {
        "session_token": token,
        "expires_at": remote.expires_at,
        "screen": _screen_payload(screen),
    }


async def resolve_remote_session(
    session: AsyncSession,
    *,
    tenant_slug: str,
    token: str,
    touch: bool = True,
) -> dict:
    if not token or not token.strip():
        raise AppError("KDS_SESSION_INVALID", "KDS session token required", 401)

    now = _utcnow()
    token_hash = hash_remote_session_token(tenant_slug, token.strip())
    remote = await session.scalar(
        select(KdsRemoteSession)
        .where(KdsRemoteSession.session_token_hash == token_hash)
        .with_for_update()
    )
    if remote is None:
        raise AppError("KDS_SESSION_INVALID", "KDS session invalid", 401)
    if remote.revoked_at is not None:
        raise AppError("KDS_SESSION_REVOKED", "KDS session revoked", 401)
    if _is_expired(remote.expires_at, now):
        raise AppError("KDS_SESSION_EXPIRED", "KDS session expired", 401)

    screen = await _get_screen_or_404(session, remote.screen_id)
    if not screen.is_active:
        raise AppError("KDS_SCREEN_INACTIVE", "KDS screen is inactive", 403, "screen_id")

    if touch:
        remote.last_seen_at = now
        await session.commit()
        await session.refresh(remote)
    return _remote_session_payload(remote, screen)


async def revoke_remote_session(
    session: AsyncSession,
    *,
    tenant_slug: str,
    token: str,
) -> dict:
    if not token or not token.strip():
        raise AppError("KDS_SESSION_INVALID", "KDS session token required", 401)

    now = _utcnow()
    token_hash = hash_remote_session_token(tenant_slug, token.strip())
    remote = await session.scalar(
        select(KdsRemoteSession)
        .where(KdsRemoteSession.session_token_hash == token_hash)
        .with_for_update()
    )
    if remote is None:
        raise AppError("KDS_SESSION_INVALID", "KDS session invalid", 401)
    if remote.revoked_at is not None:
        return {"revoked": True}
    if _is_expired(remote.expires_at, now):
        raise AppError("KDS_SESSION_EXPIRED", "KDS session expired", 401)

    remote.revoked_at = now
    await session.commit()
    return {"revoked": True}


async def revoke_screen_sessions(session: AsyncSession, *, screen_id: int) -> dict:
    await _get_screen_or_404(session, screen_id)
    result = await session.execute(
        update(KdsRemoteSession)
        .where(KdsRemoteSession.screen_id == screen_id, KdsRemoteSession.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    )
    await session.commit()
    return {"revoked_count": int(result.rowcount or 0)}
