import logging
import hashlib
import json
import time
from urllib.parse import quote

import httpx

from worker.tasks.worker_utils import with_dead_letter
from app.core.config import settings

logger = logging.getLogger(__name__)

_OVH_BASE_URLS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
}


def _ovh_base_url() -> str:
    endpoint = (settings.ovh_endpoint or "ovh-eu").strip().lower()
    if endpoint.startswith("https://"):
        return endpoint.rstrip("/")
    return _OVH_BASE_URLS.get(endpoint, "https://eu.api.ovh.com/1.0")


def _ovh_signature(method: str, query: str, body: str, timestamp: str) -> str:
    payload = "+".join([
        settings.ovh_application_secret,
        settings.ovh_consumer_key,
        method.upper(),
        query,
        body,
        timestamp,
    ])
    return "$1$" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _send_ovh_sms(*, to_phone_e164: str, body: str) -> dict:
    required = {
        "OVH_SMS_SERVICE_NAME": settings.ovh_sms_service_name,
        "OVH_APPLICATION_KEY": settings.ovh_application_key,
        "OVH_APPLICATION_SECRET": settings.ovh_application_secret,
        "OVH_CONSUMER_KEY": settings.ovh_consumer_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"OVH SMS configuration missing: {', '.join(missing)}")

    method = "POST"
    service_name = quote(settings.ovh_sms_service_name, safe="")
    query = f"{_ovh_base_url()}/sms/{service_name}/jobs"
    payload = {
        "charset": "UTF-8",
        "coding": "7bit",
        "message": body,
        "noStopClause": bool(settings.ovh_sms_no_stop_clause),
        "priority": "high",
        "receivers": [to_phone_e164],
    }
    if settings.ovh_sms_sender:
        payload["sender"] = settings.ovh_sms_sender
    body_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Ovh-Application": settings.ovh_application_key,
        "X-Ovh-Consumer": settings.ovh_consumer_key,
        "X-Ovh-Signature": _ovh_signature(method, query, body_json, timestamp),
        "X-Ovh-Timestamp": timestamp,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(query, content=body_json.encode("utf-8"), headers=headers)
    response.raise_for_status()
    return response.json()


@with_dead_letter
async def send_sms(ctx, to_phone_e164: str, body: str) -> None:
    """Send an SMS through the configured provider.

    Lot 1 establishes the queue contract. Until a real provider is configured,
    local/test environments log the message and production fails closed.
    """
    provider = (settings.sms_provider or "").strip().lower()
    if not provider:
        if (settings.environment or "").lower() == "production":
            raise RuntimeError("SMS provider is not configured")
        logger.info("SMS provider disabled", extra={"to_phone_e164": to_phone_e164, "body": body})
        return

    if provider == "ovh":
        result = await _send_ovh_sms(to_phone_e164=to_phone_e164, body=body)
        logger.info(
            "OVH SMS sent",
            extra={
                "to_phone_e164": to_phone_e164,
                "ids": result.get("ids"),
                "totalCreditsRemoved": result.get("totalCreditsRemoved"),
            },
        )
        return

    raise RuntimeError(f"Unsupported SMS provider: {provider}")
