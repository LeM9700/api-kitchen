import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


async def enqueue_sms(arq_pool, *, to_phone_e164: str, body: str) -> None:
    """Queue an SMS when a worker is available, otherwise log in dev/test.

    Real providers are intentionally behind the worker boundary so API routes
    stay fast and tests can replace the enqueue call without network access.
    """
    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job("send_sms", to_phone_e164=to_phone_e164, body=body)
            return
        except Exception:
            logger.exception("SMS enqueue failed", extra={"to_phone_e164": to_phone_e164})

    if (settings.environment or "").lower() != "production":
        logger.info("SMS delivery skipped", extra={"to_phone_e164": to_phone_e164, "body": body})
