"""Endpoints d'administration pour la sécurité WebSocket.

Accès réservé aux utilisateurs ``super_admin``.

Endpoints :
- GET    /ws-alerts              : liste paginée des alertes (filtrée par période, statut, tenant)
- PATCH  /ws-alerts/{id}/resolve : marquer une alerte comme résolue
- POST   /ban-ip                 : bannir une IP pour une durée donnée
- DELETE /ban-ip/{ip}            : lever un ban IP
- GET    /banned-ips             : lister les IPs actuellement bannies (SCAN Redis)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.http.deps import get_pagination, require_role
from app.core.http.schemas import PaginatedResponse, PaginationParams

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dépendances locales
# ---------------------------------------------------------------------------


def get_mongo(request: Request) -> AsyncIOMotorDatabase:
    """Retourne la base MongoDB partagée depuis le singleton lifespan.

    Args:
        request: Requête FastAPI courante.

    Returns:
        Instance ``AsyncIOMotorDatabase`` configurée.
    """
    from app.core.config import settings  # noqa: PLC0415
    return request.app.state.motor_client[settings.mongo_db]


def get_redis(request: Request):
    """Retourne le pool Redis arq depuis le singleton lifespan.

    Args:
        request: Requête FastAPI courante.

    Returns:
        Instance Redis (ArqRedis) prête à l'emploi.
    """
    return request.app.state.arq_pool


# ---------------------------------------------------------------------------
# Schémas Pydantic
# ---------------------------------------------------------------------------

_VALID_LAST_VALUES = {"5m", "1h", "24h"}
_LAST_TO_SECONDS = {"5m": 300, "1h": 3600, "24h": 86400}


class AlertResponse(BaseModel):
    """Représentation d'une alerte de sécurité WebSocket.

    Attributes:
        id: Identifiant MongoDB (hex string).
        event_type: Type de signal détecté.
        tenant_slug: Slug du tenant concerné.
        ip: Adresse IP source.
        user_id: Identifiant utilisateur ciblé (nullable).
        details: Données contextuelles supplémentaires.
        created_at: Date de création de l'alerte.
        resolved: Statut de résolution.
        resolved_at: Date de résolution (nullable).
        resolved_by: ID de l'admin ayant résolu (nullable).
    """

    id: str
    event_type: str
    tenant_slug: str
    ip: str
    user_id: int | None
    details: dict
    created_at: datetime
    resolved: bool
    resolved_at: datetime | None
    resolved_by: int | None


class BanIpRequest(BaseModel):
    """Corps de la requête de bannissement IP.

    Attributes:
        ip: Adresse IPv4 ou IPv6 à bannir.
        duration_minutes: Durée du ban en minutes.
        reason: Motif du bannissement (audit).
    """

    ip: str = Field(..., description="Adresse IP à bannir")
    duration_minutes: int = Field(..., ge=1, le=43200, description="Durée du ban en minutes (max 30j)")
    reason: str = Field(..., min_length=1, description="Motif du bannissement")


class BannedIpResponse(BaseModel):
    """Entrée dans la liste des IPs bannies.

    Attributes:
        ip: Adresse IP bannie.
        ttl_seconds: Secondes restantes avant expiration du ban.
        expires_at: Datetime d'expiration du ban.
    """

    ip: str
    ttl_seconds: int
    expires_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_to_alert(doc: dict) -> AlertResponse:
    """Convertit un document MongoDB en AlertResponse.

    Args:
        doc: Document brut depuis la collection ``security_alerts``.

    Returns:
        Instance AlertResponse sérialisable.
    """
    return AlertResponse(
        id=str(doc["_id"]),
        event_type=doc.get("event_type", ""),
        tenant_slug=doc.get("tenant_slug", ""),
        ip=doc.get("ip", ""),
        user_id=doc.get("user_id"),
        details=doc.get("details", {}),
        created_at=doc.get("created_at", datetime.now(timezone.utc)),
        resolved=doc.get("resolved", False),
        resolved_at=doc.get("resolved_at"),
        resolved_by=doc.get("resolved_by"),
    )


def _parse_last(last: str) -> timedelta:
    """Convertit un paramètre ``last`` (ex: "1h") en timedelta.

    Args:
        last: Valeur parmi "5m", "1h", "24h".

    Returns:
        timedelta correspondant.

    Raises:
        HTTPException: 400 si la valeur est invalide.
    """
    if last not in _VALID_LAST_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"Paramètre 'last' invalide. Valeurs acceptées : {sorted(_VALID_LAST_VALUES)}",
        )
    return timedelta(seconds=_LAST_TO_SECONDS[last])


# ---------------------------------------------------------------------------
# GET /ws-alerts
# ---------------------------------------------------------------------------


@router.get("/ws-alerts", response_model=PaginatedResponse[AlertResponse])
async def list_ws_alerts(
    request: Request,
    last: str = "1h",
    resolved: bool = False,
    tenant_slug: str | None = None,
    pagination: PaginationParams = Depends(get_pagination),
    current_user: dict = Depends(require_role("super-admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> PaginatedResponse[AlertResponse]:
    """Liste paginée des alertes de sécurité WebSocket.

    [🔒 SÉCURITÉ] Accès super_admin uniquement.

    Args:
        request: Requête FastAPI.
        last: Fenêtre temporelle ("5m", "1h", "24h"). Défaut : "1h".
        resolved: Inclure les alertes résolues (défaut : False = non résolues).
        tenant_slug: Filtrer par tenant (optionnel).
        pagination: Paramètres de pagination (page, page_size).
        current_user: Utilisateur authentifié (super_admin).
        db: Base MongoDB.

    Returns:
        PaginatedResponse contenant les AlertResponse correspondantes.
    """
    delta = _parse_last(last)
    since = datetime.now(timezone.utc) - delta

    query: dict[str, Any] = {
        "created_at": {"$gte": since},
        "resolved": resolved,
    }
    if tenant_slug:
        query["tenant_slug"] = tenant_slug

    collection = db["security_alerts"]
    total = await collection.count_documents(query)

    skip = (pagination.page - 1) * pagination.page_size
    cursor = (
        collection.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(pagination.page_size)
    )
    docs = await cursor.to_list(pagination.page_size)
    items = [_doc_to_alert(doc) for doc in docs]

    return PaginatedResponse.build(items=items, total=total, pagination=pagination)


# ---------------------------------------------------------------------------
# PATCH /ws-alerts/{alert_id}/resolve
# ---------------------------------------------------------------------------


@router.patch("/ws-alerts/{alert_id}/resolve")
async def resolve_ws_alert(
    alert_id: str,
    current_user: dict = Depends(require_role("super-admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> dict:
    """Marque une alerte de sécurité comme résolue.

    [🔒 SÉCURITÉ] Accès super_admin uniquement.

    Args:
        alert_id: Identifiant MongoDB (hex string) de l'alerte.
        current_user: Utilisateur authentifié (super_admin).
        db: Base MongoDB.

    Returns:
        Confirmation de la mise à jour.

    Raises:
        HTTPException: 400 si l'ID est invalide, 404 si l'alerte n'existe pas.
    """
    try:
        oid = ObjectId(alert_id)
    except Exception:
        raise HTTPException(status_code=400, detail="alert_id invalide (ObjectId attendu)")

    now = datetime.now(timezone.utc)
    result = await db["security_alerts"].update_one(
        {"_id": oid},
        {
            "$set": {
                "resolved": True,
                "resolved_at": now,
                "resolved_by": current_user.get("id"),
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Alerte introuvable")

    return {"resolved": True, "resolved_at": now.isoformat(), "alert_id": alert_id}


# ---------------------------------------------------------------------------
# POST /ban-ip
# ---------------------------------------------------------------------------


@router.post("/ban-ip", status_code=201)
async def ban_ip(
    body: BanIpRequest,
    current_user: dict = Depends(require_role("super-admin")),
    redis=Depends(get_redis),
) -> dict:
    """Bannit une adresse IP pour une durée déterminée.

    Stocke ``ws:ip_banned:{ip}`` dans Redis avec un TTL (duration_minutes * 60).
    Le handler WebSocket vérifie cette clé avant tout accept().

    [🔒 SÉCURITÉ] Accès super_admin uniquement.
    [⚠️ PROD] Le ban est effectif immédiatement sur toutes les instances Railway
    (Redis partagé). Les connexions existantes de l'IP ne sont pas coupées —
    seules les nouvelles tentatives sont bloquées.

    Args:
        body: IP, durée et motif du bannissement.
        current_user: Utilisateur authentifié (super_admin).
        redis: Client Redis.

    Returns:
        Dictionnaire avec ``banned_until`` (ISO datetime).
    """
    ttl_seconds = body.duration_minutes * 60
    ban_key = f"ws:ip_banned:{body.ip}"
    await redis.set(ban_key, body.reason, ex=ttl_seconds)

    banned_until = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    logger.info(
        "IP banned: ip=%s duration=%dmin reason=%s by=%s",
        body.ip, body.duration_minutes, body.reason, current_user.get("id"),
    )
    return {"banned_until": banned_until.isoformat(), "ip": body.ip}


# ---------------------------------------------------------------------------
# DELETE /ban-ip/{ip}
# ---------------------------------------------------------------------------


@router.delete("/ban-ip/{ip}", status_code=200)
async def unban_ip(
    ip: str,
    current_user: dict = Depends(require_role("super-admin")),
    redis=Depends(get_redis),
) -> dict:
    """Lève le ban d'une adresse IP.

    Supprime la clé ``ws:ip_banned:{ip}`` de Redis.

    [🔒 SÉCURITÉ] Accès super_admin uniquement.

    Args:
        ip: Adresse IP à débannir.
        current_user: Utilisateur authentifié (super_admin).
        redis: Client Redis.

    Returns:
        Confirmation avec l'IP débannie.

    Raises:
        HTTPException: 404 si l'IP n'était pas bannie.
    """
    deleted = await redis.delete(f"ws:ip_banned:{ip}")
    if not deleted:
        raise HTTPException(status_code=404, detail=f"L'IP {ip} n'est pas actuellement bannie")

    logger.info("IP unbanned: ip=%s by=%s", ip, current_user.get("id"))
    return {"unbanned": True, "ip": ip}


# ---------------------------------------------------------------------------
# GET /banned-ips
# ---------------------------------------------------------------------------


@router.get("/login-events")
async def list_login_events(
    request: Request,
    success: bool | None = None,
    email: str | None = None,
    ip_address: str | None = None,
    limit: int = 50,
    current_user: dict = Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> list[dict]:
    """Liste les evenements de connexion enregistres pour le tenant de l'admin.

    [SECURITE] Acces reserve aux admins du tenant.

    Args:
        request: Requete FastAPI.
        success: Filtre sur le statut de la tentative (True=reussie, False=echec, None=tous).
        email: Filtre exact sur l'adresse email.
        ip_address: Filtre exact sur l'adresse IP source.
        limit: Nombre max de resultats a retourner (max 200, defaut 50).
        current_user: Utilisateur authentifie (admin).
        db: Base MongoDB.

    Returns:
        Liste de documents d'evenements triee par date decroissante.
    """
    tenant_slug = current_user["tenant_slug"]
    collection = db[f"login_events_{tenant_slug}"]

    query: dict = {}
    if success is not None:
        query["success"] = success
    if email:
        query["email"] = email
    if ip_address:
        query["ip_address"] = ip_address

    effective_limit = min(limit, 200)
    docs = (
        await collection.find(query)
        .sort("created_at", -1)
        .limit(effective_limit)
        .to_list(effective_limit)
    )
    for doc in docs:
        doc.pop("_id", None)
        if "created_at" in doc and hasattr(doc["created_at"], "isoformat"):
            doc["created_at"] = doc["created_at"].isoformat()
    return docs


@router.get("/banned-ips", response_model=list[BannedIpResponse])
async def list_banned_ips(
    current_user: dict = Depends(require_role("super-admin")),
    redis=Depends(get_redis),
) -> list[BannedIpResponse]:
    """Liste toutes les IPs actuellement bannies avec leur TTL restant.

    [🔒 SÉCURITÉ] Accès super_admin uniquement.
    [⚠️ PROD] Utilise SCAN (itératif, non bloquant) plutôt que KEYS pour éviter
    de bloquer le thread Redis event loop en production sous forte charge.

    Args:
        current_user: Utilisateur authentifié (super_admin).
        redis: Client Redis.

    Returns:
        Liste des BannedIpResponse avec ip, ttl_seconds et expires_at.
    """
    results: list[BannedIpResponse] = []
    pattern = "ws:ip_banned:*"
    now = datetime.now(timezone.utc)

    # [⚠️ PROD] SCAN itératif — COUNT=100 est une suggestion au serveur Redis,
    # pas une garantie. Chaque itération peut retourner 0..N clés.
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        for key in keys:
            try:
                ttl = await redis.ttl(key)
                if ttl <= 0:
                    # Clé expirée entre le SCAN et le TTL — on ignore.
                    continue
                # Extraire l'IP depuis la clé "ws:ip_banned:{ip}"
                ip = key[len("ws:ip_banned:"):]
                expires_at = now + timedelta(seconds=ttl)
                results.append(
                    BannedIpResponse(ip=ip, ttl_seconds=ttl, expires_at=expires_at)
                )
            except Exception as exc:
                logger.error("list_banned_ips: erreur TTL pour key=%s: %s", key, exc)
        if cursor == 0:
            break

    results.sort(key=lambda r: r.ttl_seconds)
    return results
