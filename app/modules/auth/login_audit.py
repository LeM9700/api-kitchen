"""MongoDB login event audit helper.

This module provides a single async function that writes login events to a
per-tenant MongoDB collection. All errors are swallowed — audit is non-critical
and must never break the login flow.

Usage (non-blocking):
    asyncio.create_task(log_login_event(db, tenant_slug, ...))
"""

from datetime import datetime, timezone


async def log_login_event(
    db,
    tenant_slug: str,
    email: str,
    user_id: int | None,
    success: bool,
    failure_reason: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Write a login event document to the per-tenant MongoDB collection.

    Non-blocking by design — callers should wrap with asyncio.create_task().
    All exceptions are caught and silently discarded.

    Args:
        db: AsyncIOMotorDatabase instance (motor).
        tenant_slug: Tenant identifier; collection will be ``login_events_{tenant_slug}``.
        email: Email address used in the login attempt.
        user_id: User's primary key if authentication succeeded, None on failure.
        success: True if login was successful, False otherwise.
        failure_reason: AppError code string on failure (e.g. "INVALID_CREDENTIALS"), or None.
        ip_address: Client IP extracted from x-forwarded-for or request.client.host.
        user_agent: HTTP User-Agent header value.
    """
    try:
        collection = db[f"login_events_{tenant_slug}"]
        doc = {
            "tenant_slug": tenant_slug,
            "email": email,
            "user_id": user_id,
            "success": success,
            "failure_reason": failure_reason,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "created_at": datetime.now(timezone.utc),
        }
        await collection.insert_one(doc)
    except Exception:
        pass  # Audit is non-critical — never propagate errors to the caller
