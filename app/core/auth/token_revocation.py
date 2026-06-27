"""JTI deny-list and user-disabled flag helpers backed by Redis (arq pool).

All functions accept an arq-compatible Redis client (ArqRedis) and issue raw
Redis commands.  No imports from app.core.http.deps or app.core.auth.security to avoid
circular dependencies.
"""
from datetime import datetime, timezone


async def revoke_jti(redis, jti: str, expires_at: datetime) -> None:
    """Store a revoked JTI in Redis until the token would have expired.

    Args:
        redis: ArqRedis instance (app.state.arq_pool).
        jti: JWT ID claim from the access token.
        expires_at: Absolute expiry time of the token (timezone-aware UTC).
    """
    now = datetime.now(timezone.utc)
    ttl = max(1, int((expires_at - now).total_seconds()))
    await redis.setex(f"jti:{jti}", ttl, "1")


async def is_jti_revoked(redis, jti: str) -> bool:
    """Return True if the JTI is present in the deny-list.

    Args:
        redis: ArqRedis instance.
        jti: JWT ID to check.

    Returns:
        True when the key exists (token revoked), False otherwise.
    """
    return bool(await redis.exists(f"jti:{jti}"))


async def flag_user_disabled(redis, user_id: int) -> None:
    """Set a short-lived flag marking a user account as disabled.

    The key expires after 86400 seconds (24 h) so stale flags self-clean.
    Callers should refresh the flag on each relevant admin action.

    Args:
        redis: ArqRedis instance.
        user_id: Primary-key of the user to disable.
    """
    await redis.set(f"user_disabled:{user_id}", "1", ex=86400)


async def is_user_disabled(redis, user_id: int) -> bool:
    """Return True if the user has an active disabled flag in Redis.

    Args:
        redis: ArqRedis instance.
        user_id: Primary-key of the user.

    Returns:
        True when disabled flag exists, False otherwise.
    """
    return bool(await redis.exists(f"user_disabled:{user_id}"))


async def clear_user_disabled(redis, user_id: int) -> None:
    """Remove the disabled flag from Redis, re-enabling the user.

    Args:
        redis: ArqRedis instance.
        user_id: Primary-key of the user.
    """
    await redis.delete(f"user_disabled:{user_id}")
