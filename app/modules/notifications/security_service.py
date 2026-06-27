"""Security monitoring service for WebSocket connections.
Detects suspicious patterns and alerts super admins.

Signaux surveillés :
- ip_flood           : > 10 tentatives de connexion depuis la même IP en 5 min.
- jwt_bruteforce     : > 3 tokens JWT invalides/expirés depuis la même IP en 5 min.
- credential_stuffing: un même user_id ciblé depuis > 3 IPs distinctes en 5 min.

Chaque signal déclenche :
1. Déduplication Redis (TTL 24h) — une seule alerte par (tenant, type, IP) par jour.
2. Stockage MongoDB dans la collection globale ``security_alerts``.
3. Notification WebSocket de tous les users ``super_admin`` actifs.
"""

import logging
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select

from app.core.database import get_public_session
from app.modules.auth.models import User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seuils & TTLs
# ---------------------------------------------------------------------------

IP_RATE_LIMIT_THRESHOLD: int = 10     # max tentatives IP / 5 min
JWT_BRUTEFORCE_THRESHOLD: int = 3     # max JWT invalides / 5 min
CREDENTIAL_STUFFING_THRESHOLD: int = 3  # max IPs distinctes par user / 5 min
WINDOW_SECONDS: int = 300              # fenêtre glissante = 5 minutes
ALERT_DEDUP_TTL: int = 86400           # déduplication = 24 h

# ---------------------------------------------------------------------------
# Messages descriptifs par type d'alerte
# ---------------------------------------------------------------------------

_ALERT_BODIES: dict[str, str] = {
    "ip_flood": (
        "L'IP {ip} a tenté plus de {threshold} connexions WebSocket "
        "en 5 minutes sur le tenant {tenant}."
    ),
    "jwt_bruteforce": (
        "L'IP {ip} a présenté plus de {threshold} tokens JWT invalides/expirés "
        "en 5 minutes sur le tenant {tenant}."
    ),
    "credential_stuffing": (
        "Le compte #{user_id} a été ciblé depuis plus de {threshold} adresses IP "
        "distinctes en 5 minutes sur le tenant {tenant}."
    ),
}


# ---------------------------------------------------------------------------
# Fonctions de détection (appelées depuis ws_router)
# ---------------------------------------------------------------------------


async def check_and_record_jwt_bruteforce(
    redis,
    ip: str,
    tenant_slug: str,
    mongo_db=None,
) -> bool:
    """Incrémente le compteur JWT invalide pour cette IP et déclenche une alerte si seuil dépassé.

    Appelé depuis ws_router après les close codes 4001 (invalid JWT) ou 4002 (expired JWT).
    Clé Redis : ``ws:ip_invalid_jwt:{ip}`` avec EXPIRE 300 s.

    [🔒 SÉCURITÉ] N'interrompt jamais le flux principal (try/except global).

    Args:
        redis: Client Redis (app.state.arq_pool).
        ip: Adresse IP du client.
        tenant_slug: Slug du tenant concerné.
        mongo_db: Base MongoDB pour le stockage d'alertes (None = skip MongoDB).

    Returns:
        True si le seuil est dépassé et une alerte a été déclenchée, False sinon.
    """
    try:
        key = f"ws:ip_invalid_jwt:{ip}"
        count = await redis.incr(key)
        if count == 1:
            # [⚠️ PROD] EXPIRE positionné au premier incrément pour éviter
            # une clé sans TTL si EXPIRE échoue au mauvais moment.
            await redis.expire(key, WINDOW_SECONDS)
        if count > JWT_BRUTEFORCE_THRESHOLD:
            await record_ws_event(
                redis=redis,
                event_type="jwt_bruteforce",
                ip=ip,
                tenant_slug=tenant_slug,
                extra={"invalid_jwt_count": count},
                mongo_db=mongo_db,
            )
            return True
        return False
    except Exception as exc:
        logger.error("check_and_record_jwt_bruteforce error ip=%s: %s", ip, exc)
        return False


async def check_and_record_credential_stuffing(
    redis,
    ip: str,
    tenant_slug: str,
    user_id: int,
    mongo_db=None,
) -> bool:
    """Enregistre l'IP ayant tenté de se connecter avec ce user_id et déclenche une alerte si seuil dépassé.

    Appelé depuis ws_router après validation JWT réussie (user_id connu).
    Clé Redis : ``ws:user_ips:{tenant_slug}:{user_id}`` avec EXPIRE 300 s.

    [🔒 SÉCURITÉ] N'interrompt jamais le flux principal (try/except global).

    Args:
        redis: Client Redis (app.state.arq_pool).
        ip: Adresse IP du client.
        tenant_slug: Slug du tenant concerné.
        user_id: Identifiant de l'utilisateur ciblé.
        mongo_db: Base MongoDB pour le stockage d'alertes (None = skip MongoDB).

    Returns:
        True si le seuil est dépassé et une alerte a été déclenchée, False sinon.
    """
    try:
        key = f"ws:user_ips:{tenant_slug}:{user_id}"
        await redis.sadd(key, ip)
        await redis.expire(key, WINDOW_SECONDS)
        count = await redis.scard(key)
        if count > CREDENTIAL_STUFFING_THRESHOLD:
            await record_ws_event(
                redis=redis,
                event_type="credential_stuffing",
                ip=ip,
                tenant_slug=tenant_slug,
                user_id=user_id,
                extra={"distinct_ip_count": count},
                mongo_db=mongo_db,
            )
            return True
        return False
    except Exception as exc:
        logger.error(
            "check_and_record_credential_stuffing error user_id=%s ip=%s: %s",
            user_id, ip, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Fonction principale d'enregistrement d'alerte
# ---------------------------------------------------------------------------


async def record_ws_event(
    redis,
    event_type: Literal["ip_flood", "jwt_bruteforce", "credential_stuffing"],
    ip: str,
    tenant_slug: str,
    user_id: int | None = None,
    extra: dict | None = None,
    mongo_db=None,
) -> None:
    """Stocke une alerte de sécurité WebSocket et notifie les super admins.

    Flux d'exécution :
    1. Déduplication Redis (TTL 24h) — sortie immédiate si déjà alerté.
    2. Insertion dans MongoDB ``security_alerts`` (si mongo_db fourni).
    3. Notification WebSocket de tous les users ``super_admin`` actifs.

    [🔒 SÉCURITÉ] N'interrompt jamais le flux principal (try/except global).
    [⚠️ PROD] La déduplication Redis évite le spam d'alertes pour un signal persistant.

    Args:
        redis: Client Redis (app.state.arq_pool).
        event_type: Type de signal détecté.
        ip: Adresse IP source.
        tenant_slug: Slug du tenant concerné.
        user_id: Identifiant utilisateur ciblé (pertinent pour credential_stuffing).
        extra: Données contextuelles supplémentaires (compteurs, etc.).
        mongo_db: Base MongoDB (AsyncIOMotorDatabase). None = skip stockage MongoDB.
    """
    try:
        # --- Déduplication ---
        # [⚠️ PROD] SET NX pour atomicité : évite la double alerte sous charge.
        alert_key = f"ws:alert:{tenant_slug}:{event_type}:{ip}"
        inserted = await redis.set(alert_key, "1", ex=ALERT_DEDUP_TTL, nx=True)
        if not inserted:
            logger.debug(
                "Security alert deduplicated: type=%s tenant=%s ip=%s",
                event_type, tenant_slug, ip,
            )
            return

        now = datetime.now(timezone.utc)
        alert_doc = {
            "event_type": event_type,
            "tenant_slug": tenant_slug,
            "ip": ip,
            "user_id": user_id,
            "details": extra or {},
            "created_at": now,
            "resolved": False,
            "resolved_at": None,
            "resolved_by": None,
        }

        # --- Stockage MongoDB ---
        if mongo_db is not None:
            try:
                result = await mongo_db["security_alerts"].insert_one(alert_doc)
                logger.info(
                    "Security alert stored: type=%s tenant=%s ip=%s mongo_id=%s",
                    event_type, tenant_slug, ip, result.inserted_id,
                )
            except Exception as exc:
                logger.error("Security alert MongoDB insert failed: %s", exc)

        # --- Notification super admins ---
        await _notify_super_admins(
            redis=redis,
            event_type=event_type,
            ip=ip,
            tenant_slug=tenant_slug,
            user_id=user_id,
        )

    except Exception as exc:
        logger.error(
            "record_ws_event error: type=%s tenant=%s ip=%s: %s",
            event_type, tenant_slug, ip, exc,
        )


# ---------------------------------------------------------------------------
# Notification super admins
# ---------------------------------------------------------------------------


async def _notify_super_admins(
    redis,
    event_type: str,
    ip: str,
    tenant_slug: str,
    user_id: int | None = None,
) -> None:
    """Envoie une notification WebSocket à tous les users super_admin actifs.

    Utilise get_public_session pour chercher les super admins dans ``public.users``.
    Import lazy de notify_user pour éviter les imports circulaires
    (ws_router → security_service → notification_service → ws_router).

    [🔒 SÉCURITÉ] N'interrompt jamais le flux principal (try/except global).

    Args:
        redis: Client Redis pour la publication pub/sub.
        event_type: Type de signal déclenché.
        ip: Adresse IP source.
        tenant_slug: Slug du tenant concerné.
        user_id: Identifiant utilisateur ciblé (peut être None).
    """
    try:
        # Import lazy pour éviter les imports circulaires au niveau module.
        from app.modules.notifications.notification_service import notify_user  # noqa: PLC0415

        threshold_map = {
            "ip_flood": IP_RATE_LIMIT_THRESHOLD,
            "jwt_bruteforce": JWT_BRUTEFORCE_THRESHOLD,
            "credential_stuffing": CREDENTIAL_STUFFING_THRESHOLD,
        }
        body = _ALERT_BODIES.get(event_type, "Alerte sécurité WebSocket.").format(
            ip=ip,
            tenant=tenant_slug,
            user_id=user_id,
            threshold=threshold_map.get(event_type, "?"),
        )

        async with get_public_session() as session:
            result = await session.execute(
                select(User).where(
                    User.role == "super_admin",
                    User.is_active.is_(True),
                )
            )
            super_admins = list(result.scalars())

        if not super_admins:
            logger.warning("No active super_admin found to notify for security alert")
            return

        for admin in super_admins:
            try:
                # [⚠️ PROD] Chaque notify_user ouvre sa propre session push/DB.
                # On passe redis pour la diffusion pub/sub multi-instance Railway.
                async with get_public_session() as session:
                    await notify_user(
                        session=session,
                        tenant_slug=tenant_slug,
                        user_id=admin.id,
                        event="security_alert",
                        title=f"⚠️ Alerte sécurité — {tenant_slug}",
                        body=body,
                        data={
                            "alert_type": event_type,
                            "ip": ip,
                            "tenant_slug": tenant_slug,
                        },
                        redis=redis,
                    )
            except Exception as exc:
                logger.error(
                    "Failed to notify super_admin id=%s: %s", admin.id, exc
                )

    except Exception as exc:
        logger.error("_notify_super_admins error: type=%s ip=%s: %s", event_type, ip, exc)
