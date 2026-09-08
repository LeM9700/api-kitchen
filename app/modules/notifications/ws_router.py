"""WebSocket endpoint pour les notifications temps réel.

Architecture :
- Auth en 2 temps : connexion sans token, serveur envoie auth_required,
  client repond avec le JWT (jamais en query param - invisible dans les logs).
- Connexions stockees localement (_connections) ET dans Redis (SADD/SREM/SCARD)
  pour le decompte cross-instance Railway.
- Redis pub/sub (redis.asyncio, PAS aioredis) : _redis_subscriber ecoute
  notif:* (diffusion de notifications) et session_revoked:* (fermeture
  immediate suite a une desactivation/revocation/retrait de permissions,
  voir app.core.auth.token_revocation.publish_session_revoked). Demarree et
  annulee proprement par le lifespan de app.main:lifespan -- une seule tache
  par instance, stockee sur app.state.ws_redis_subscriber_task.
- Heartbeat serveur : ping toutes les 30s, fermeture zombie si pong absent
  dans 10s ; a chaque cycle, relit aussi l'etat AUTORITAIRE en PostgreSQL
  (role, permissions triees, is_active -- voir WsAuthState/_fetch_ws_auth_state)
  et le compare au snapshot capture a l'authentification (_ws_close_reason),
  en plus des flags Redis rapides quand Redis est disponible. Un retrait de
  permission, un changement de role, une desactivation ou une suppression
  de compte ferment donc la WebSocket (code 4009) sous HEARTBEAT_INTERVAL
  secondes au pire, MEME SI Redis est indisponible ou si le signal pub/sub
  session_revoked a ete manque -- PostgreSQL est le mecanisme de secours
  autoritaire, Redis restant le mecanisme rapide. Un access token HTTP
  expire n'a par lui-meme aucun effet sur une WebSocket deja etablie : c'est
  ce heartbeat, pas l'expiration du JWT, qui borne l'effet d'un tel
  changement sur une connexion deja ouverte.
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
from typing import NamedTuple

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, WebSocketException
from jwt.exceptions import ExpiredSignatureError, PyJWTError as JWTError

from app.core.config import settings
from app.core.auth.token_revocation import is_jti_revoked, is_user_disabled
from app.core.auth.security import decode_token
from app.core.database import get_public_session
from app.core.http.deps import get_client_ip_ws
from app.core.tenancy.tenant import get_live_tenant_user_state, user_belongs_to_tenant

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


async def _close_user_connections(tenant_slug: str, user_id: int, code: int, reason: str) -> None:
    """Ferme immediatement toutes les WebSockets locales d'un utilisateur.

    [🔒 SÉCURITÉ] Appelee par ``_redis_subscriber`` a la reception d'un
    message sur le canal ``session_revoked:*`` (voir
    ``app.core.auth.token_revocation.publish_session_revoked``), publie
    lorsqu'un compte est desactive, ses permissions modifiees, ou toutes ses
    sessions revoquees. Ne touche que les connexions de l'instance courante --
    en multi-instance Railway, chaque instance recoit le meme message pub/sub
    et ferme ses propres connexions locales. Le nettoyage de l'etat
    (``_connections``, ``ws:connections:*`` Redis) est laisse au bloc
    ``finally`` de ``notifications_ws`` : fermer la socket ici suffit a faire
    lever ``WebSocketDisconnect`` dans la boucle ``_ws_handler``.

    Args:
        tenant_slug: Slug du tenant.
        user_id: Identifiant de l'utilisateur dont les connexions doivent fermer.
        code: Code de fermeture WebSocket a envoyer.
        reason: Motif court transmis au client.
    """
    key = f"{tenant_slug}:{user_id}"
    async with _connections_lock:
        sockets = set(_connections.get(key, set()))

    for ws in sockets:
        try:
            await ws.close(code=code, reason=reason)
        except Exception as exc:
            logger.debug("_close_user_connections: echec fermeture key=%s: %s", key, exc)


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
    """Coroutine background : ecoute notif:*/session_revoked:* sur Redis et dispatche localement.

    Demarree depuis le lifespan de ``app.main`` (``app.state.ws_redis_subscriber_task``)
    et annulee proprement au shutdown -- une seule instance de cette coroutine
    par process API, jamais recreee tant que le process vit (voir le lifespan
    pour la garde contre un double-demarrage). Cree une connexion Redis dediee
    (le pool arq ne supporte pas pubsub). Reconnexion automatique avec retry
    toutes les secondes en cas de coupure Redis.

    [🔒 SÉCURITÉ] Utilise ``redis.asyncio`` (le paquet ``redis>=5.0`` deja une
    dependance du projet, voir ``pyproject.toml`` et ``app.main::health_ready``
    pour le meme import) -- PAS ``aioredis`` (paquet tiers absent des
    dependances, abandonne en amont au profit de ``redis-py``'s built-in
    asyncio support depuis la 4.2).
    """
    from redis.asyncio import from_url as redis_from_url

    while True:
        redis = None
        pubsub = None
        try:
            redis = redis_from_url(settings.redis_url, decode_responses=True)
            pubsub = redis.pubsub()
            await pubsub.psubscribe("notif:*", "session_revoked:*")
            logger.info("Redis subscriber: abonne a notif:* et session_revoked:*")

            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "pmessage":
                    continue
                channel: str = raw_msg["channel"]  # "notif:{tenant_slug}:{user_id}" ou "session_revoked:{tenant_slug}:{user_id}"
                try:
                    parts = channel.split(":", 2)
                    if len(parts) != 3:
                        continue
                    prefix, tenant_slug, user_id_str = parts
                    if prefix == "session_revoked":
                        payload = json.loads(raw_msg["data"])
                        await _close_user_connections(
                            tenant_slug,
                            int(user_id_str),
                            code=4009,
                            reason=payload.get("reason", "session_revoked"),
                        )
                        continue
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


class WsAuthState(NamedTuple):
    """Snapshot normalise, comparable par egalite, de l'etat tenant autoritaire
    d'un utilisateur au moment ou une WebSocket est authentifiee.

    [🔒 SÉCURITÉ] ``permissions`` est un tuple TRIE (pas la liste brute
    ``users.permissions``) pour que la comparaison d'egalite entre deux
    snapshots ne depende pas de l'ordre de stockage en base -- seul un
    changement de CONTENU (ajout/retrait d'une permission) doit declencher
    une fermeture, jamais un simple reordonnancement sans effet.
    """

    role: str
    permissions: tuple[str, ...]
    is_active: bool


async def _fetch_ws_auth_state(tenant_slug: str, user_id: int) -> WsAuthState | None:
    """Lit l'etat tenant AUTORITAIRE (PostgreSQL) et le normalise en ``WsAuthState``.

    [🔒 SÉCURITÉ] Ne fait JAMAIS confiance aux claims du JWT (``role``,
    ``permissions``) pour cette comparaison -- ils sont fige a l'emission du
    token et peuvent etre perimes des la seconde suivante (retrait de
    permission, changement de role). Seule une lecture fraiche via
    ``get_live_tenant_user_state`` (meme fonction que
    ``app.core.http.deps.get_current_user`` cote HTTP) fait foi.

    Args:
        tenant_slug: Slug du tenant.
        user_id: Identifiant de l'utilisateur.

    Returns:
        ``WsAuthState`` si l'utilisateur existe dans ce schema tenant,
        ``None`` sinon (compte supprime).
    """
    live_state = await get_live_tenant_user_state(user_id, tenant_slug)
    if live_state is None:
        return None
    return WsAuthState(
        role=live_state.role,
        permissions=tuple(sorted(live_state.permissions or [])),
        is_active=bool(live_state.is_active),
    )


async def _ws_close_reason(
    redis, tenant_slug: str, user_id: int, jti: str | None, auth_state: WsAuthState | None
) -> str | None:
    """Revalide, a un instant donne, si une WebSocket doit etre fermee.

    [🔒 SÉCURITÉ] Deux sources, complementaires :
    1. Redis (rapide, optionnel) : flags ``is_user_disabled``/``is_jti_revoked``
       -- poses par ``flag_user_disabled``/``revoke_jti``, disparaissent si
       Redis est indisponible ou a ete vide. Sert de raccourci pour les cas
       de desactivation/logout deja couverts par un signal explicite ; leur
       absence ne bloque jamais la revalidation PostgreSQL ci-dessous.
    2. PostgreSQL (autoritaire, TOUJOURS consulte, meme quand Redis a repondu
       "rien a signaler" ou est indisponible) : relit l'etat courant via
       ``_fetch_ws_auth_state`` et le compare au snapshot ``auth_state``
       capture a l'authentification de cette connexion (voir
       ``notifications_ws``). Toute difference -- compte supprime, compte
       desactive, changement de role, AJOUT OU RETRAIT d'une permission --
       declenche la fermeture. C'est ce mecanisme, pas l'expiration du JWT
       HTTP (qui n'a aucun effet sur une WebSocket deja etablie), qui borne
       dans le temps l'effet d'un retrait de permissions sur une connexion
       WS deja ouverte, meme si Redis est indisponible ou si le signal
       pub/sub ``session_revoked`` a ete manque.

    Args:
        redis: ArqRedis instance ou None si indisponible.
        tenant_slug: Slug du tenant.
        user_id: Identifiant de l'utilisateur authentifie sur cette WS.
        jti: ``jti`` de l'access token ayant authentifie cette WS (optionnel).
        auth_state: Snapshot capture a l'authentification de cette connexion,
            ou ``None`` pour un flux sans etat tenant a comparer (jeton
            super-admin plateforme -- voir ``notifications_ws``), auquel cas
            seule la revalidation Redis ci-dessus s'applique, inchangee par
            rapport au comportement precedent pour ce flux.

    Returns:
        Motif court (str) si la connexion doit etre fermee, ``None`` sinon.
    """
    if redis is not None:
        try:
            if await is_user_disabled(redis, user_id, tenant_slug):
                return "account_disabled"
            if jti and await is_jti_revoked(redis, jti):
                return "token_revoked"
        except Exception as exc:
            logger.debug(
                "WS revalidation Redis error: tenant=%s user_id=%s error=%s",
                tenant_slug, user_id, exc,
            )

    if auth_state is None:
        return None

    try:
        current_state = await _fetch_ws_auth_state(tenant_slug, user_id)
    except Exception as exc:
        # [⚠️ PROD] Fail-open sur une erreur PostgreSQL transitoire : une
        # coupure passagere de la base ne doit pas fermer en masse toutes les
        # WebSockets ouvertes a chaque cycle de heartbeat. Le prochain cycle
        # (au plus HEARTBEAT_INTERVAL secondes plus tard) retentera la lecture.
        logger.debug(
            "WS revalidation PostgreSQL error: tenant=%s user_id=%s error=%s",
            tenant_slug, user_id, exc,
        )
        return None

    if current_state is None:
        return "account_deleted"
    if not current_state.is_active:
        return "account_disabled"
    if current_state.role != auth_state.role:
        return "role_changed"
    if current_state.permissions != auth_state.permissions:
        return "permissions_changed"
    return None


async def _ws_handler(
    websocket: WebSocket,
    tenant_slug: str,
    user_id: int,
    connection_id: str,
    auth_state: WsAuthState | None,
    redis=None,
    jti: str | None = None,
) -> None:
    """Boucle principale WebSocket avec heartbeat serveur.

    Attend des messages entrants avec un timeout de HEARTBEAT_INTERVAL secondes.
    A chaque expiration, envoie un ping et attend le pong dans HEARTBEAT_TIMEOUT
    secondes. Ferme la connexion zombie si le pong n'arrive pas.

    [🔒 SÉCURITÉ] A chaque cycle de heartbeat (au plus HEARTBEAT_INTERVAL
    secondes, 30s par defaut), relit l'etat AUTORITAIRE en PostgreSQL --
    role, permissions (triees) et is_active, voir ``_fetch_ws_auth_state`` --
    et le compare au snapshot ``auth_state`` capture a l'authentification de
    cette connexion (voir ``notifications_ws``). Cette comparaison est
    TOUJOURS effectuee, meme quand Redis a repondu "rien a signaler" ou est
    totalement indisponible : PostgreSQL est le mecanisme de SECOURS
    autoritaire, Redis (flags ``is_user_disabled``/``is_jti_revoked`` et
    signal pub/sub ``session_revoked:*``, voir ``_close_user_connections``
    et ``publish_session_revoked``) restant le mecanisme de fermeture RAPIDE
    (quasi temps reel, cross-instance) quand il est disponible. Toute
    difference -- compte supprime, compte desactive, changement de role, ou
    AJOUT/RETRAIT d'une permission -- ferme la connexion avec le code 4009.
    Le pire delai avant fermeture est donc borne a HEARTBEAT_INTERVAL, y
    compris pour un retrait de permissions sans desactivation et y compris
    si le signal pub/sub a ete manque ou si Redis est totalement indisponible
    -- CE heartbeat, PAS l'expiration du JWT HTTP, est ce qui borne l'effet
    d'un tel retrait sur une WebSocket deja etablie : un access token
    expire cote HTTP n'a par lui-meme AUCUN effet sur une connexion
    WebSocket deja ouverte (voir ``_ws_close_reason`` pour le detail des
    deux sources).

    [🔒 SÉCURITÉ] Ne fait JAMAIS confiance aux claims ``role``/``permissions``
    du JWT ayant servi a l'authentification initiale pour cette comparaison
    -- ``auth_state`` est lui-meme deja un snapshot PostgreSQL (jamais un
    decodage du JWT), et chaque relecture au heartbeat l'est aussi.

    Les messages {"type": "pong"} sont consommes silencieusement.
    Les autres types de messages entrants sont ignores (protocole unidirectionnel
    serveur -> client).

    Args:
        websocket: Connexion WebSocket authentifiee.
        tenant_slug: Slug du tenant (pour les logs).
        user_id: Identifiant de l'utilisateur authentifie (pour les logs).
        connection_id: UUID hex de cette connexion (pour les logs).
        auth_state: Snapshot ``WsAuthState`` capture a l'authentification de
            cette connexion (voir ``notifications_ws``), reference pour
            detecter tout changement de role/permissions/is_active. ``None``
            pour un flux sans etat tenant a comparer (jeton super-admin
            plateforme), auquel cas seule la revalidation Redis s'applique.
        redis: ArqRedis instance, pour la revalidation periodique rapide (optionnel).
        jti: ``jti`` de l'access token ayant authentifie cette connexion.
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
            # Aucun message depuis HEARTBEAT_INTERVAL -> revalider la session puis pinguer.
            close_reason = await _ws_close_reason(redis, tenant_slug, user_id, jti, auth_state)
            if close_reason is not None:
                logger.info(
                    "WS ferme (%s): user_id=%s tenant=%s conn=%s",
                    close_reason, user_id, tenant_slug, connection_id,
                )
                try:
                    await websocket.close(code=4009, reason=close_reason)
                except Exception:
                    pass
                break

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

    try:
        user_id: int = int(payload.get("sub"))
    except (TypeError, ValueError):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Invalid subject",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    payload_tenant: str = str(payload.get("tenant_slug", ""))

    if payload_tenant != str(tenant_slug):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Tenant mismatch",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    jti = payload.get("jti")
    if jti and await is_jti_revoked(redis, str(jti)):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Token revoked",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    if await is_user_disabled(redis, user_id, payload_tenant):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Account disabled",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # [SECURITE] Revalide que le sub appartient bien au tenant reclame -- meme
    # controle que app.core.http.deps.get_current_user, necessaire ici car ce
    # handler WS ne passe pas par get_current_user (voir user_belongs_to_tenant).
    if payload_tenant and not await user_belongs_to_tenant(user_id, payload_tenant, payload.get("email")):
        await websocket.send_json({
            "type": "error",
            "code": "unauthorized",
            "reason": "Unauthorized",
        })
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # [🔒 SÉCURITÉ] ``auth_state`` reste ``None`` pour un token super-admin
    # (``role="super-admin"``, ``payload_tenant`` vide) : ce flux n'a pas de
    # ligne dans ``tenant_{slug}.users`` a comparer -- voir la note dans
    # ``_ws_close_reason`` sur ce cas ecarte du perimetre de ce filet.
    auth_state: WsAuthState | None = None

    if payload.get("role") != "super-admin" and payload_tenant:
        from sqlalchemy import text as _text

        async with get_public_session() as pub_session:
            row = await pub_session.execute(
                _text(
                    "SELECT is_suspended, suspension_message "
                    "FROM public.tenants WHERE slug = :slug"
                ),
                {"slug": payload_tenant},
            )
            tenant_row = row.fetchone()
            if tenant_row and tenant_row.is_suspended:
                await websocket.send_json({
                    "type": "error",
                    "code": "forbidden",
                    "reason": tenant_row.suspension_message or "Tenant suspended",
                })
                await websocket.close(code=4003, reason="Tenant suspended")
                return

        # [🔒 SÉCURITÉ] Capture le snapshot AUTORITAIRE (PostgreSQL) role +
        # permissions (triees) + is_active a l'instant de l'authentification --
        # jamais les claims du JWT, potentiellement perimes des la seconde
        # suivante. Ce snapshot sert de reference pour ``_ws_handler`` : tout
        # ecart detecte a un heartbeat ulterieur (retrait de permission,
        # changement de role, desactivation, suppression) ferme la connexion.
        auth_state = await _fetch_ws_auth_state(payload_tenant, user_id)
        if auth_state is None or not auth_state.is_active:
            await websocket.send_json({
                "type": "error",
                "code": "unauthorized",
                "reason": "Account disabled",
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
        await _ws_handler(
            websocket, tenant_slug, user_id, connection_id, auth_state,
            redis=redis, jti=str(jti) if jti else None,
        )
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
