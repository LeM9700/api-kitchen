"""Service push multi-plateforme : APNs (iOS) et FCM v1 (Android).

Architecture :
- APNs : HTTP/2 via httpx, auth JWT ES256 signé avec la clé privée P-256 d'Apple.
  Le token JWT est mis en cache 55 min (renouvellement automatique avant la limite
  Apple de 60 min).
- FCM v1 : HTTP/1.1 via httpx, auth OAuth2 Bearer via google-auth service account.
  Le token OAuth2 est renouvellé automatiquement par la bibliothèque google-auth.
- Un token invalide (410 APNs, UNREGISTERED FCM) marque is_active=False en base.
- Les erreurs par token sont isolées : un échec n'annule pas les autres envois.

[⚠️ PROD] Les clients httpx sont créés à la volée par send_push_notification.
Pour un volume élevé (>100 notifs/s), envisager des singletons module-level
avec httpx.AsyncClient partagé et connection pooling explicite.
"""

import json
import logging
import time
from datetime import datetime, timezone

import httpx
import jwt  # PyJWT
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.notifications.models import DeviceToken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APNs JWT cache (ES256, renouvelé toutes les 55 min)
# ---------------------------------------------------------------------------
_apns_token: str | None = None
_apns_token_issued_at: float = 0.0
_APNS_TOKEN_TTL: float = 55 * 60  # secondes


def _load_apns_private_key() -> str:
    """Charge la clé privée APNs depuis un Secret File Railway ou une env var.

    Priorité :
    1. ``APNS_PRIVATE_KEY_PATH`` → lit le fichier (Railway Secret File, recommandé prod).
    2. ``APNS_PRIVATE_KEY``      → contenu PEM direct (env var, dev local).

    Les ``\\n`` littéraux présents dans les env vars sont normalisés en vrais sauts
    de ligne pour que PyJWT parse correctement la clé PEM.

    Returns:
        Contenu PEM de la clé privée ES256.

    Raises:
        RuntimeError: Si aucune des deux sources n'est configurée.
        OSError: Si le fichier pointé par ``APNS_PRIVATE_KEY_PATH`` est illisible.
    """
    if settings.apns_private_key_path:
        with open(settings.apns_private_key_path) as fh:
            return fh.read()
    if settings.apns_private_key:
        return settings.apns_private_key.replace("\\n", "\n")
    raise RuntimeError(
        "APNs private key not configured. "
        "Set APNS_PRIVATE_KEY_PATH (Railway Secret File) "
        "or APNS_PRIVATE_KEY (env var)."
    )


def _build_apns_token() -> str:
    """Génère un JWT ES256 pour l'authentification APNs.

    Apple impose :
    - Algorithme ES256 (P-256 EC) — la clé .p8 est une clé EC privée.
    - Header ``kid`` = key_id, ``alg`` = ``ES256``.
    - Claims ``iss`` = team_id, ``iat`` = timestamp Unix courant.
    - Validité max 60 min → on renouvelle à 55 min.

    Returns:
        JWT signé en ES256 prêt à être envoyé en header ``Authorization: Bearer``.

    Raises:
        RuntimeError: Si les paramètres APNs ne sont pas configurés.
    """
    if not (settings.apns_key_id and settings.apns_team_id):
        raise RuntimeError("APNs not configured (APNS_KEY_ID, APNS_TEAM_ID)")

    private_key = _load_apns_private_key()

    return jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(time.time())},
        private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )


def _get_apns_token() -> str:
    """Retourne le JWT APNs en cache, ou en génère un nouveau si expiré.

    Returns:
        JWT APNs valide (fraîcheur < 55 min).
    """
    global _apns_token, _apns_token_issued_at
    if _apns_token is None or (time.time() - _apns_token_issued_at) > _APNS_TOKEN_TTL:
        _apns_token = _build_apns_token()
        _apns_token_issued_at = time.time()
    return _apns_token


# ---------------------------------------------------------------------------
# FCM OAuth2 token (google-auth, renouvellement automatique)
# ---------------------------------------------------------------------------
_fcm_credentials = None  # google.oauth2.service_account.Credentials


def _get_fcm_access_token() -> str:
    """Récupère un access token OAuth2 pour FCM v1.

    Utilise google.oauth2.service_account.Credentials avec le scope
    ``https://www.googleapis.com/auth/firebase.messaging``.
    La bibliothèque google-auth gère le renouvellement automatique via
    ``credentials.expired``.

    [⚠️ PROD] ``credentials.refresh()`` est synchrone et fait un appel réseau.
    Wrappé dans ``anyio.to_thread.run_sync`` dans ``_send_fcm``.

    Returns:
        Access token OAuth2 Bearer valide.

    Raises:
        RuntimeError: Si FCM n'est pas configuré.
    """
    global _fcm_credentials

    if not (settings.fcm_project_id and settings.fcm_service_account_json):
        raise RuntimeError("FCM not configured (FCM_PROJECT_ID, FCM_SERVICE_ACCOUNT_JSON)")

    if _fcm_credentials is None:
        from google.oauth2 import service_account  # import tardif — optionnel au runtime

        _fcm_credentials = service_account.Credentials.from_service_account_info(
            json.loads(settings.fcm_service_account_json),
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )

    if not _fcm_credentials.valid or _fcm_credentials.expired:
        import google.auth.transport.requests

        _fcm_credentials.refresh(google.auth.transport.requests.Request())

    return _fcm_credentials.token


# ---------------------------------------------------------------------------
# Envoi APNs
# ---------------------------------------------------------------------------
async def _send_apns(
    session: AsyncSession,
    device_token: DeviceToken,
    title: str,
    body: str,
    data: dict,
) -> bool:
    """Envoie une notification push à un appareil iOS via APNs HTTP/2.

    Args:
        session: Session SQLAlchemy active (pour invalider le token si 410).
        device_token: Enregistrement DeviceToken ciblé.
        title: Titre de la notification.
        body: Corps de la notification.
        data: Données custom envoyées avec le payload (order_id, event…).

    Returns:
        True si l'envoi a réussi, False sinon.
    """
    if not settings.apns_bundle_id:
        logger.warning("APNs bundle_id not configured, skipping iOS push")
        return False

    use_sandbox = settings.environment != "production"
    base_url = (
        "https://api.sandbox.push.apple.com"
        if use_sandbox
        else "https://api.push.apple.com"
    )
    url = f"{base_url}/3/device/{device_token.token}"

    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "badge": 1,
        },
        **data,
    }
    headers = {
        "authorization": f"bearer {_get_apns_token()}",
        "apns-topic": settings.apns_bundle_id,
        "apns-push-type": "alert",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(http2=True) as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)

        if response.status_code == 200:
            device_token.last_used_at = datetime.now(timezone.utc)
            await session.commit()
            return True

        if response.status_code == 410:
            # [⚠️ PROD] Token révoqué par Apple — désactivation immédiate.
            logger.info(
                "APNs 410 — token invalide désactivé: user_id=%s device_id=%s",
                device_token.user_id,
                device_token.id,
            )
            device_token.is_active = False
            await session.commit()
            return False

        logger.warning(
            "APNs HTTP %s pour device_id=%s: %s",
            response.status_code,
            device_token.id,
            response.text[:200],
        )
        return False

    except Exception as exc:
        logger.error("APNs send error device_id=%s: %s", device_token.id, exc)
        return False


# ---------------------------------------------------------------------------
# Envoi FCM v1
# ---------------------------------------------------------------------------
async def _send_fcm(
    session: AsyncSession,
    device_token: DeviceToken,
    title: str,
    body: str,
    data: dict,
) -> bool:
    """Envoie une notification push à un appareil Android via FCM v1.

    Args:
        session: Session SQLAlchemy active (pour invalider le token si UNREGISTERED).
        device_token: Enregistrement DeviceToken ciblé.
        title: Titre de la notification.
        body: Corps de la notification.
        data: Données custom envoyées dans le champ ``data`` FCM (valeurs str).

    Returns:
        True si l'envoi a réussi, False sinon.
    """
    if not settings.fcm_project_id:
        logger.warning("FCM project_id not configured, skipping Android push")
        return False

    url = (
        f"https://fcm.googleapis.com/v1/projects"
        f"/{settings.fcm_project_id}/messages:send"
    )

    # [⚠️ PROD] FCM data values doivent être des strings.
    str_data = {k: str(v) for k, v in data.items()}

    payload = {
        "message": {
            "token": device_token.token,
            "notification": {"title": title, "body": body},
            "data": str_data,
            "android": {"priority": "high"},
        }
    }

    try:
        # _get_fcm_access_token() peut appeler refresh() synchrone — on wrappe.
        import anyio

        access_token = await anyio.to_thread.run_sync(_get_fcm_access_token)
        headers = {
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)

        if response.status_code == 200:
            device_token.last_used_at = datetime.now(timezone.utc)
            await session.commit()
            return True

        # Détection token invalide FCM.
        error_code = ""
        try:
            error_code = response.json().get("error", {}).get("details", [{}])[0].get(
                "errorCode", ""
            )
        except Exception:
            pass

        if error_code == "UNREGISTERED" or response.status_code == 404:
            # [⚠️ PROD] Token FCM non enregistré — désactivation immédiate.
            logger.info(
                "FCM UNREGISTERED — token invalide désactivé: user_id=%s device_id=%s",
                device_token.user_id,
                device_token.id,
            )
            device_token.is_active = False
            await session.commit()
            return False

        logger.warning(
            "FCM HTTP %s pour device_id=%s: %s",
            response.status_code,
            device_token.id,
            response.text[:200],
        )
        return False

    except Exception as exc:
        logger.error("FCM send error device_id=%s: %s", device_token.id, exc)
        return False


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------
async def send_push_notification(
    session: AsyncSession,
    tenant_slug: str,
    user_id: int,
    title: str,
    body: str,
    data: dict,
) -> dict:
    """Envoie une notification push à tous les appareils actifs d'un utilisateur.

    Dispatche vers APNs (iOS) ou FCM v1 (Android) selon la plateforme de chaque
    token enregistré. Les erreurs par token sont isolées : un échec n'annule pas
    les autres envois.

    [⚠️ PROD] Cette fonction ne lève jamais d'exception — les erreurs sont loguées.
    L'appelant reçoit un résumé {"sent": int, "failed": int}.

    Args:
        session: Session SQLAlchemy async dans le schéma tenant courant.
        tenant_slug: Slug du tenant (pour contexte de log).
        user_id: Identifiant de l'utilisateur cible.
        title: Titre de la notification push.
        body: Corps de la notification push.
        data: Données supplémentaires (order_id, event, notification_id…).

    Returns:
        Dict ``{"sent": int, "failed": int}`` résumant l'envoi.
    """
    result = await session.execute(
        select(DeviceToken).where(
            DeviceToken.user_id == user_id,
            DeviceToken.is_active.is_(True),
        )
    )
    tokens = list(result.scalars())

    if not tokens:
        return {"sent": 0, "failed": 0}

    sent = 0
    failed = 0

    for token in tokens:
        success = False
        try:
            if token.platform == "ios":
                success = await _send_apns(session, token, title, body, data)
            elif token.platform == "android":
                success = await _send_fcm(session, token, title, body, data)
            else:
                logger.warning("Unknown platform '%s' for device_id=%s", token.platform, token.id)
        except Exception as exc:
            logger.error(
                "send_push_notification: unhandled error tenant=%s user_id=%s device_id=%s: %s",
                tenant_slug,
                user_id,
                token.id,
                exc,
            )

        if success:
            sent += 1
        else:
            failed += 1

    return {"sent": sent, "failed": failed}
