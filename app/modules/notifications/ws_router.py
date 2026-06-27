"""WebSocket endpoint pour les notifications temps réel.

Architecture :
- Auth en 2 temps : connexion sans token, serveur envoie auth_required,
  client repond avec le JWT (jamais en query param - invisible dans les logs).
- Connexions stockees localement (_connections) ET dans Redis (SADD/SREM/SCARD)
  pour le decompte cross-instance Railway.
- Redis pub/sub : _redis_subscriber ecoute notif:* et dispatche aux WS locaux.
  A demarrer depuis le lifespan de main.py.
- Heartbeat serveur : ping toutes les 30s, fermeture zombie si pong absent dans 10s.
- Limite de 5 connexions simultanees par user (SCARD + Lock par user).

Sécurité IP (ordre d'exécution au début de notifications_ws) :
1. Extraction IP réelle (support X-Forwarded-For Railway/proxy).
2. Vérification ban IP (Redis ws:ip_banned:{ip}) → close 4006 si banni.
3. Rate limit IP (Redis ws:ip_attempts:{ip}) → WebSocketException(1008) si > 10/5min.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, WebSocketException
from jwt.exceptions import ExpiredSignatureError, PyJWTError as JWTError

from app.core.config import settings
from app.core.auth.security import decode_token
from app.core.http.deps import get_client_ip_ws

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# State local
# ---------------------------------------------------------------------------

# Connexions actives sur cette instance : "{tenant_slug}:{user_id}" -> set[WebSocket]
_connections: dict[str, set[WebSocket]] = {}
_connections_lock = asyncio.Lock()

# Un Lock par cle (tenant_slug:user_id) pour serialiser SCARD + SADD Redis.
_user_locks: dict[str, asyncio.Lock] = {}
_user_locks_meta_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MAX_CONNECTIONS_PER_USER: int = 5
AUTH_TIMEOUT: float = 10.0    # secondes pour recevoir le message auth
HEARTBEAT_INTERVAL: int = 30  # secondes entre deux pings serveur
HEARTBEAT_TIMEOUT: int = 10   # secondes pour recevoir le pong

# [🔒 SÉCURITÉ] Seuils de sécurité IP
IP_RATE_LIMIT_MAX: int = 10    # max tentatives / fenêtre
IP_RATE_LIMIT_WINDOW: int = 300  # fenêtre = 5 min


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Retourne l'heure UTC courante en ISO 8601.

    Returns:
        Chaine ISO 8601 de l'instant UTC courant.
    """
    return datetime.now(timezone.utc).isoformat()


def _get_client_ip(websocket: WebSocket) -> str:
    """Extrait l'adresse IP réelle du client.

    [🔒 SÉCURITÉ] Délègue à ``get_client_ip_ws`` depuis ``app.core.http.deps``.
    Avec ``--proxy-headers`` sur uvicorn (Railway), ``websocket.client.host``
    est déjà l'IP réelle résolue par le ProxyHeadersMiddleware — lire directement
    X-Forwarded-For serait spoofable par n'importe quel client.

    Args:
        websocket: Connexion WebSocket FastAPI/Starlette.

    Returns:
        Adresse IP du client, ou "unknown" si indisponible.
    """
    return get_client_ip_ws(websocket)


async def _get_user_lock(key: str) -> asyncio.Lock:
    """Retourne (ou cree) le asyncio.Lock dedie a une cle user.

    Garantit qu'une seule coroutine a la fois effectue la sequence
    SCARD -> SADD (ou SREM) sur Redis, evitant les race conditions de comptage.

    Args:
        key: Cle de la forme "{tenant_slug}:{user_id}".

    Returns:
        Lock associe a cette cle (cree si absent).
    """
    async with _user_locks_meta_lock:
        if key not in _user_locks:
            _user_locks[key] = asyncio.Lock()
        return _user_locks[key]


# ---------------------------------------------------------------------------
# Broadcast local (appele par le subscriber Redis)
# ---------------------------------------------------------------------------


async def broadcast_to_user(
    tenant_slug: str,
    user_id: int,
    message: dict,
) -> int:
    """Diffuse un message JSON aux connexions WebSocket locales d'un user.

    Ne touche que les WebSockets de l'instance courante. En multi-instance
    Railway, les autres instances recoivent le message via _redis_subscriber.

    Args:
        tenant_slug: Slug du tenant.
        user_id: Identifiant de l'utilisateur cible.
        message: Dictionnaire JSON a envoyer.

    Returns:
        Nombre de WebSockets ayant recu le message avec succes.
    """
    key = f"{tenant_slug}:{user_id}"

    async with _connections_lock:
        sockets = set(_connections.get(key, set()))  # copie pour iterer hors lock

    if not sockets:
        return 0

    sent = 0
    for ws in sockets:
        try:
            await ws.send_json(message)
            sent += 1
        except Exception as exc:
            logger.debug("broadcast_to_user: echec envoi key=%s: %s", key, exc)

    return sent


# ---------------------------------------------------------------------------
# Redis subscriber -- scaling horizontal Railway
# ---------------------------------------------------------------------------


async def _redis_subscriber() -> None:
    """Coroutine background : ecoute notif:* sur Redis et dispatche localement.

    Doit etre demarree depuis le lifespan de main.py et annulee au shutdown.
    Cree une connexion Redis dedicee (le pool arq ne supporte pas pubsub).
    Reconnexion automatique avec retry toutes les secondes en cas de coupure Redis.

    Exemple d'integration dans main.py::

        from app.modules.notifications import ws_router

        @asynccontextmanager
        async def lifespan(app):
            task = asyncio.create_task(ws_router._redis_subscriber())
            yield
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    """
    import aioredis

    while True:
        redis = None
        pubsub = None
        try:
            redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
            pubsub = redis.pubsub()
            await pubsub.psubscribe("notif:*")
            logger.info("Redis subscriber: abonne a notif:*")

            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "pmessage":
                    continue
                channel: str = raw_msg["channel"]  # "notif:{tenant_slug}:{user_id}"
                try:
                    parts = channel.split(":", 2)
                    if len(parts) != 3:
                        continue
                    _, tenant_slug, user_id_str = parts
                    message = json.loads(raw_msg["data"])
                    await broadcast_to_user(tenant_slug, int(user_id_str), message)
                except Exception as exc:
                    logger.debug(
                        "redis_subscriber: dispatch error channel=%s: %s", channel, exc
                    )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Redis subscriber: connexion perdue -- retry dans 1s: %s", exc)
            await asyncio.sleep(1)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.close()
                except Exception:
                    pass
            if redis is not None:
                try:
                    await redis.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Heartbeat + boucle principale WebSocket
# ---------------------------------------------------------------------------


async def _ws_handler(
    websocket: WebSocket,
    tenant_slug: str,
    user_id: int,
    connection_id: str,
) -> None:
    """Boucle principale WebSocket avec heartbeat serveur.

    Attend des messages entrants avec un timeout de HEARTBEAT_INTERVAL secondes.
    A chaque expiration, envoie un ping et attend le pong dans HEARTBEAT_TIMEOUT
    secondes. Ferme la connexion zombie si le pong n'arrive pas.

    Les messages {"type": "pong"} sont consommes silencieusement.
    Les autres types de messages entrants sont ignores (protocole unidirectionnel
    serveur -> client).

    Args:
        websocket: Connexion WebSocket authentifiee.
        tenant_slug: Slug du tenant (pour les logs).
        user_id: Identifiant de l'utilisateur authentifie (pour les logs).
        connection_id: UUID hex de cette connexion (pour les logs).
    """
    while True:
        try:
            msg = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=float(HEARTBEAT_INTERVAL),
            )
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "pong":
                continue  # connexion vivante
            logger.debug(
                "WS message entrant inattendu: type=%s conn=%s",
                msg.get("type"),
                connection_id,
            )

        except asyncio.TimeoutError:
            # Aucun message depuis HEARTBEAT_INTERVAL -> envoyer un ping
            try:
                await websocket.send_json({"type": "ping", "timestamp": _utcnow()})
            except Exception:
                break  # WebSocket deja ferme cote reseau

            try:
                pong = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=float(HEARTBEAT_TIMEOUT),
                )
                if not isinstance(pong, dict) or pong.get("type") != "pong":
                    logger.debug("WS zombie (mauvaise reponse ping): conn=%s", connection_id)
                    break
            except asyncio.TimeoutError:
                logger.debug("WS zombie (timeout pong): conn=%s", connection_id)
                break
            except Exception:
                break

        except WebSocketDisconnect:
            break
        except (ValueError, TypeError):
            # JSON invalide envoye par le client -- on ignore.
            continue
        except Exception as exc:
            logger.debug("WS erreur handler: conn=%s error=%s", connection_id, exc)
            break


# ---------------------------------------------------------------------------
# Endpoint WebSocket
# ---------------------------------------------------------------------------


@router.websocket("/ws/notifications")
async def notifications_ws(
    websocket: WebSocket,
    tenant_slug: str = Query(..., description="Slug du tenant (identifiant public)"),
) -> None:
    """Endpoint WebSocket pour la reception de notifications temps reel.

    Sécurité IP (avant handshake ou en tout début de session) :
    - Vérification ban IP → close 4006 si banni.
    - Rate limit IP (> 10/5min) → WebSocketException(1008) avant accept().

    Protocole d'authentification en 2 temps -- le JWT ne transite jamais en
    query param (non visible dans les logs Railway/nginx) :

    1. Le client ouvre la connexion WebSocket sans token.
    2. Le serveur envoie {"type": "auth_required"}.
    3. Le client envoie {"type": "auth", "token": "<jwt>"}.
    4. Resultats possibles :
       - Token invalide -> {"type": "error", "code": "unauthorized"} + close(4001)
       - Token expire   -> {"type": "error", "code": "token_expired"} + close(4002)
       - Timeout 10s    -> close(4003, "Auth timeout")
       - Trop de conns  -> {"type": "error", "code": "too_many_connections"} + close(4004)
       - IP rate limited -> {"type": "error", "code": "IP_RATE_LIMITED"} + close(4005)
       - IP bannie       -> {"type": "error", "code": "IP_BANNED"} + close(4006)
       - Valide          -> {"type": "auth_ok", "user_id": <id>}

    Heartbeat : le serveur envoie {"type": "ping"} toutes les 30s. Le client doit
    repondre {"type": "pong"} dans les 10s, sinon la connexion est fermee.

    Format des messages sortants (serveur -> client)::

        {
            "type": "notification",
            "event": "order.confirmed",
            "title": "Commande confirmee",
            "body": "Votre commande #42 a ete confirmee.",
            "data": {"order_id": 42, "notification_id": "..."},
            "notification_id": "...",
            "timestamp": "2026-06-20T12:00:00Z"
        }

    Args:
        websocket: Connexion WebSocket FastAPI/Starlette.
        tenant_slug: Slug du tenant en query param (identifiant public, non sensible).
    """
    redis = websocket.app.state.arq_pool

    # Résolution MongoDB pour les alertes sécurité (best-effort, peut être None)
    try:
        from app.core.config import settings as _settings
        mongo_db = websocket.app.state.motor_client[_settings.mongo_db]
    except Exception:
        mongo_db = None

    # Import lazy du service sécurité (évite import circulaire au niveau module)
    from app.modules.notifications.security_service import (  # noqa: PLC0415
        check_and_record_credential_stuffing,
        check_and_record_jwt_bruteforce,
        record_ws_event,
    )

    # -------------------------------------------------------------------------
    # Étape 0 : extraction IP réelle (Railway/proxy)
    # -------------------------------------------------------------------------
    client_ip = _get_client_ip(websocket)

    # -------------------------------------------------------------------------
    # Étape 1 : vérification ban IP (AVANT accept())
    # [🔒 SÉCURITÉ] Les IPs bannies sont rejetées avant tout handshake WS.
    # On doit accept() pour envoyer un message JSON, puis close(4006).
    # -------------------------------------------------------------------------
    if await redis.exists(f"ws:ip_banned:{client_ip}"):
        ttl = await redis.ttl(f"ws:ip_banned:{client_ip}")
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "code": "IP_BANNED",
            "retry_after": max(ttl, 0),
            "message": "Votre adresse IP est bannie. Contactez l'administrateur.",
        })
        await websocket.close(code=4006, reason="ip_banned")
        logger.warning("WS rejected: IP banned ip=%s ttl=%s", client_ip, ttl)
        return

    # -------------------------------------------------------------------------
    # Étape 2 : rate limit IP (AVANT accept())
    # [🔒 SÉCURITÉ] Rejoint AVANT le handshake pour ne pas consommer de ressources.
    # [⚠️ PROD] INCR + EXPIRE atomique au premier incrément évite les fuites de clé.
    # -------------------------------------------------------------------------
    ip_attempts_key = f"ws:ip_attempts:{client_ip}"
    attempt_count = await redis.incr(ip_attempts_key)
    if attempt_count == 1:
        await redis.expire(ip_attempts_key, IP_RATE_LIMIT_WINDOW)

    if attempt_count > IP_RATE_LIMIT_MAX:
        retry_after = await redis.ttl(ip_attempts_key)
        retry_after = max(retry_after, 0)

        # Alerte super admin au premier dépassement uniquement (count == 11)
        if attempt_count == IP_RATE_LIMIT_MAX + 1:
            try:
                await record_ws_event(
                    redis=redis,
                    event_type="ip_flood",
                    ip=client_ip,
                    tenant_slug=tenant_slug,
                    extra={"attempt_count": attempt_count},
                    mongo_db=mongo_db,
                )
            except Exception as exc:
                logger.error("record_ws_event ip_flood failed: %s", exc)

        logger.warning(
            "WS rejected: IP rate limited ip=%s count=%s retry_after=%s",
            client_ip, attempt_count, retry_after,
        )
        # [🔒 SÉCURITÉ] Rejet AVANT accept() — le client reçoit HTTP 403 (1008 policy violation).
        raise WebSocketException(code=1008, reason="rate_limited")

    # -------------------------------------------------------------------------
    # accept() — handshake WebSocket effectif
    # -------------------------------------------------------------------------
    await websocket.accept()

    # -------------------------------------------------------------------------
    # Phase 1 : demande d'authentification
    # -------------------------------------------------------------------------
    await websocket.send_json({"type": "auth_required"})

    try:
        auth_msg = await asyncio.wait_for(
            websocket.receive_json(), timeout=AUTH_TIMEOUT
        )
    except asyncio.TimeoutError:
        await websocket.close(code=4003, reason="Auth timeout")
        return
    except Exception:
        await websocket.close(code=4001, reason="Invalid message")
        return

    if not isinstance(auth_msg, dict) or auth_msg.get("type") != "auth":
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Expected auth message with token",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    token: str = auth_msg.get("token", "")

    # -------------------------------------------------------------------------
    # Phase 2 : validation JWT
    # -------------------------------------------------------------------------
    try:
        payload = decode_token(token)
    except ExpiredSignatureError:
        await websocket.send_json({
            "type": "error",
            "code": "token_expired",
            "reason": "Token expired",
        })
        await websocket.close(code=4002, reason="Token expired")
        # [🔒 SÉCURITÉ] Signal 2 — JWT bruteforce : token expiré depuis cette IP
        try:
            await check_and_record_jwt_bruteforce(
                redis=redis, ip=client_ip, tenant_slug=tenant_slug, mongo_db=mongo_db,
            )
        except Exception as exc:
            logger.error("check_and_record_jwt_bruteforce failed: %s", exc)
        return
    except JWTError:
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Invalid token",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        # [🔒 SÉCURITÉ] Signal 2 — JWT bruteforce : token invalide depuis cette IP
        try:
            await check_and_record_jwt_bruteforce(
                redis=redis, ip=client_ip, tenant_slug=tenant_slug, mongo_db=mongo_db,
            )
        except Exception as exc:
            logger.error("check_and_record_jwt_bruteforce failed: %s", exc)
        return

    if payload.get("type") != "access":
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Invalid token type",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    user_id: int = int(payload.get("sub"))
    payload_tenant: str = str(payload.get("tenant_slug", ""))

    if payload_tenant != str(tenant_slug):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Tenant mismatch",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # -------------------------------------------------------------------------
    # [🔒 SÉCURITÉ] Signal 3 — Credential stuffing : enregistrer l'IP pour ce user
    # Déclenché après validation JWT complète (user_id et tenant confirmés).
    # -------------------------------------------------------------------------
    try:
        await check_and_record_credential_stuffing(
            redis=redis,
            ip=client_ip,
            tenant_slug=tenant_slug,
            user_id=user_id,
            mongo_db=mongo_db,
        )
    except Exception as exc:
        logger.error("check_and_record_credential_stuffing failed: %s", exc)

    # -------------------------------------------------------------------------
    # Phase 3 : limite de connexions par user (SCARD + Lock pour eviter race)
    # -------------------------------------------------------------------------
    connection_id = uuid.uuid4().hex
    redis_conn_key = f"ws:connections:{tenant_slug}:{user_id}"
    user_key = f"{tenant_slug}:{user_id}"

    user_lock = await _get_user_lock(user_key)
    async with user_lock:
        count = await redis.scard(redis_conn_key)
        if count >= MAX_CONNECTIONS_PER_USER:
            await websocket.send_json({
                "type": "error",
                "code": "too_many_connections",
                "reason": f"Maximum {MAX_CONNECTIONS_PER_USER} connexions simultanees par compte.",
            })
            await websocket.close(code=4004, reason="Too many connections")
            return
        # SADD + EXPIRE dans le meme lock pour coherence
        await redis.sadd(redis_conn_key, connection_id)
        await redis.expire(redis_conn_key, 86400)  # TTL 24h -- guard contre les fuites

    # -------------------------------------------------------------------------
    # Phase 4 : enregistrement local + confirmation auth
    # -------------------------------------------------------------------------
    async with _connections_lock:
        if user_key not in _connections:
            _connections[user_key] = set()
        _connections[user_key].add(websocket)

    await websocket.send_json({"type": "auth_ok", "user_id": user_id})
    logger.info(
        "WS connecte: user_id=%s tenant=%s conn=%s ip=%s",
        user_id, tenant_slug, connection_id, client_ip,
    )

    # -------------------------------------------------------------------------
    # Boucle principale (heartbeat + reception messages)
    # -------------------------------------------------------------------------
    try:
        await _ws_handler(websocket, tenant_slug, user_id, connection_id)
    except WebSocketDisconnect:
        logger.info(
            "WS deconnecte: user_id=%s tenant=%s conn=%s",
            user_id, tenant_slug, connection_id,
        )
    except Exception as exc:
        logger.debug(
            "WS erreur: user_id=%s tenant=%s conn=%s error=%s",
            user_id, tenant_slug, connection_id, exc,
        )
    finally:
        # Nettoyage connexion locale
        async with _connections_lock:
            sockets = _connections.get(user_key)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    del _connections[user_key]

        # Nettoyage Redis (dans le meme Lock pour coherence avec SCARD/SADD)
        async with user_lock:
            await redis.srem(redis_conn_key, connection_id)

        logger.info(
            "WS nettoye: user_id=%s tenant=%s conn=%s",
            user_id, tenant_slug, connection_id,
        )
