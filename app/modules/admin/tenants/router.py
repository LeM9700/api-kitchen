"""Router FastAPI pour le tableau de bord tenant self-service.

Routes publiques (pas d'auth) :
- GET /tenant/status   -- statut operationnel courant
- GET /tenant/hours    -- creneaux horaires (affichage vitrine)

Routes admin :
- GET    /tenant/config
- PATCH  /tenant/config
- GET    /tenant/audit
- PUT    /tenant/hours/{day}
- DELETE /tenant/hours/{day}
- GET    /tenant/closures
- POST   /tenant/closures
- DELETE /tenant/closures/{id}
"""
from math import ceil

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, get_client_ip, require_role
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse
from app.core.services.cache import get_cached_json, set_cached_json
from app.modules.admin.tenants import service as tenant_service
from app.modules.admin.tenants.schemas import (
    BusinessHoursCreate,
    BusinessHoursResponse,
    ExceptionalClosureCreate,
    ExceptionalClosureResponse,
    NextOpeningResponse,
    TenantBrandingResponse,
    TenantBrandingUpdate,
    TenantClosureToggle,
    TenantConfigAuditResponse,
    TenantConfigResponse,
    TenantScheduledClosureRequest,
    TenantConfigUpdate,
    TenantStatusResponse,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Routes publiques — branding
# ---------------------------------------------------------------------------


@router.get(
    "/branding",
    response_model=TenantBrandingResponse,
    summary="Branding public du tenant (sans auth)",
    description=(
        "Retourne les données de branding (couleurs, logo, nom d'affichage) pour le tenant "
        "identifié par le query param `tenant_slug`. "
        "Endpoint public — aucune authentification requise. "
        "Appelé par l'app Flutter au boot pour charger le thème avant toute navigation."
    ),
)
async def get_tenant_branding(
    tenant_slug: str = Query(..., description="Slug du tenant"),
) -> TenantBrandingResponse:
    """GET /tenant/branding — public, sans auth.

    [⚠️ PROD] Ne retourne que TenantBrandingResponse — ne jamais exposer
    TenantConfig complet ici.

    Args:
        tenant_slug: Slug du tenant (query param).

    Returns:
        TenantBrandingResponse (5 champs branding, tous nullable).
    """
    async with get_tenant_session(tenant_slug) as session:
        return await tenant_service.get_branding(session)


# ---------------------------------------------------------------------------
# Routes publiques
# ---------------------------------------------------------------------------


def _tenant_status_cache_key(tenant_slug: str) -> str:
    return f"tenant:status:{tenant_slug}"


@router.get("/status", response_model=TenantStatusResponse)
async def get_tenant_status(
    tenant_slug: str = Query(..., description="Slug du tenant"),
    redis=Depends(get_arq_pool),
) -> TenantStatusResponse:
    """Retourne le statut operationnel courant du restaurant.

    Endpoint public -- ne requiert aucune authentification.
    Utilise par la vitrine cliente pour afficher l'etat d'ouverture.

    [PERF] Mis en cache 10s -- endpoint a fort volume (consulte a chaque
    ouverture d'app cliente) et faible frequence de changement reelle. Un
    changement de statut (fermeture manuelle, etc.) est deja notifie en temps
    reel via WebSocket (``notify_config_change``) independamment de ce cache ;
    celui-ci n'affecte que le polling HTTP, pas la notification push.

    Args:
        tenant_slug: Slug du tenant (query param).

    Returns:
        TenantStatusResponse avec is_open, prep_time, message, next_opening.
    """
    cache_key = _tenant_status_cache_key(tenant_slug)
    cached = await get_cached_json(redis, cache_key)
    if cached is not None:
        return cached

    async with get_tenant_session(tenant_slug) as session:
        result = await tenant_service.get_tenant_status(session)

    await set_cached_json(redis, cache_key, result.model_dump(mode="json"), ttl_seconds=10)
    return result


@router.get("/next-opening", response_model=NextOpeningResponse)
async def get_next_opening(
    tenant_slug: str = Query(..., description="Slug du tenant"),
) -> NextOpeningResponse:
    """Retourne le prochain horaire d'ouverture sans statut complet.

    Endpoint public -- utilise par la vitrine pour afficher "Rouvre lundi a 11h".

    Args:
        tenant_slug: Slug du tenant (query param).

    Returns:
        NextOpeningResponse avec next_opening en clair ou None.
    """
    async with get_tenant_session(tenant_slug) as session:
        result = await tenant_service.get_next_opening(session)
    return NextOpeningResponse(next_opening=result)


@router.get("/hours", response_model=list[BusinessHoursResponse])
async def list_business_hours(
    tenant_slug: str = Query(..., description="Slug du tenant"),
) -> list[BusinessHoursResponse]:
    """Retourne tous les creneaux horaires du restaurant.

    Endpoint public -- utilise par la vitrine pour afficher les horaires.

    Args:
        tenant_slug: Slug du tenant (query param).

    Returns:
        Liste de BusinessHoursResponse triee par (day_of_week, slot_index).
    """
    async with get_tenant_session(tenant_slug) as session:
        return await tenant_service.get_business_hours(session)


# ---------------------------------------------------------------------------
# Routes admin -- branding
# ---------------------------------------------------------------------------


@router.patch(
    "/branding",
    response_model=TenantBrandingResponse,
    summary="Mettre à jour le branding du tenant (admin)",
)
@limiter.limit("30/minute")
async def patch_tenant_branding(
    request: Request,
    body: TenantBrandingUpdate,
    current_user: dict = Depends(require_role("admin")),
) -> TenantBrandingResponse:
    """PATCH /tenant/branding — admin uniquement.

    Mise à jour partielle du branding (patch sémantique — seuls les champs
    non-None sont modifiés). Chaque champ modifié est tracé dans l'audit.

    Args:
        request: Requête FastAPI (requis par SlowAPI).
        body: TenantBrandingUpdate avec les champs à modifier.
        current_user: Utilisateur admin injecté par dépendance.

    Returns:
        TenantBrandingResponse après mise à jour.
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tenant_service.update_branding(
            session,
            body,
            user_id=current_user["id"],
            user_email=current_user.get("email"),
            ip_address=ip,
            user_agent=user_agent,
        )


# ---------------------------------------------------------------------------
# Routes admin -- config
# ---------------------------------------------------------------------------


@router.get("/config", response_model=TenantConfigResponse)
async def get_config(
    current_user: dict = Depends(require_role("admin")),
) -> TenantConfigResponse:
    """Retourne la configuration tenant courante.

    Args:
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        TenantConfigResponse complete.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tenant_service.get_or_create_config(session)


@router.patch("/config", response_model=TenantConfigResponse)
@limiter.limit("30/minute")
async def patch_config(
    request: Request,
    body: TenantConfigUpdate,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantConfigResponse:
    """Met a jour partiellement la configuration tenant.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        body: Champs a mettre a jour.
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        TenantConfigResponse mise a jour.
    """
    # [SECURITE] X-Forwarded-For prioritaire sur l'IP directe (reverse proxy).
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await tenant_service.update_config(
            session,
            body,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
            user_email=current_user.get("email"),
            arq_pool=arq_pool,
            tenant_slug=current_user["tenant_slug"],
        )
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
    return result


@router.patch("/toggle-closure", response_model=TenantConfigResponse)
@limiter.limit("5/minute")
async def toggle_closure(
    request: Request,
    body: TenantClosureToggle,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantConfigResponse:
    """Bascule l'etat de fermeture manuelle du restaurant.

    [SECURITE] Endpoint dedie rate-limite a 5/min pour eviter le spam
    open/close. Le cooldown 2 min est egalement applique cote service.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        body: TenantClosureToggle avec is_temporarily_closed et message optionnel.
        current_user: Utilisateur admin injecte par dependance.
        arq_pool: Pool ARQ pour enqueue la notification.

    Returns:
        TenantConfigResponse mise a jour.
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    update_data = TenantConfigUpdate(
        is_temporarily_closed=body.is_temporarily_closed,
        temporary_closure_message=body.temporary_closure_message,
    )

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await tenant_service.update_config(
            session,
            update_data,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
            user_email=current_user.get("email"),
            arq_pool=arq_pool,
            tenant_slug=current_user["tenant_slug"],
        )
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
    return result


@router.put("/scheduled-closure", response_model=TenantConfigResponse)
@limiter.limit("30/minute")
async def schedule_closure(
    request: Request,
    body: TenantScheduledClosureRequest,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantConfigResponse:
    """Planifie ou annule une fermeture automatique du restaurant."""
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await tenant_service.schedule_closure(
            session,
            body,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
            user_email=current_user.get("email"),
        )
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
    return result


# ---------------------------------------------------------------------------
# Routes admin -- audit log
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=PaginatedResponse[TenantConfigAuditResponse])
async def get_audit_log(
    limit: int = Query(50, ge=1, le=200, description="Nombre d'entrees a retourner"),
    offset: int = Query(0, ge=0, description="Decalage pour la pagination"),
    current_user: dict = Depends(require_role("admin")),
) -> PaginatedResponse[TenantConfigAuditResponse]:
    """Retourne l'historique pagine des modifications de configuration.

    [SECURITE] Reserve aux admins -- expose les adresses IP et user-agents.

    Args:
        limit: Nombre maximum d'entrees (max 200).
        offset: Decalage pour la pagination.
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        PaginatedResponse[TenantConfigAuditResponse] triee par date decroissante.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await tenant_service.get_audit_log(session, limit=limit, offset=offset)

    pages = max(1, ceil(total / limit))
    page = (offset // limit) + 1
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=limit,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# Routes admin -- horaires
# ---------------------------------------------------------------------------


@router.put("/hours/{day}", response_model=list[BusinessHoursResponse])
@limiter.limit("30/minute")
async def replace_business_hours(
    request: Request,
    day: int,
    slots: list[BusinessHoursCreate],
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> list[BusinessHoursResponse]:
    """Remplace tous les creneaux d'un jour de la semaine.

    Supprime les anciens creneaux du jour puis insere les nouveaux.
    Valide l'absence de chevauchements avant ecriture.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        day: Jour de la semaine (0=lundi, 6=dimanche).
        slots: Nouveaux creneaux pour ce jour.
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        Liste des BusinessHoursResponse crees.
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await tenant_service.upsert_business_hours(
            session,
            day,
            slots,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
        )
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
    return result


@router.delete("/hours/{day}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_business_hours(
    request: Request,
    day: int,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> None:
    """Supprime tous les creneaux d'un jour de la semaine.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        day: Jour de la semaine (0=lundi, 6=dimanche).
        current_user: Utilisateur admin injecte par dependance.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await tenant_service.delete_business_hours_day(session, day)
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))


# ---------------------------------------------------------------------------
# Routes admin -- fermetures exceptionnelles
# ---------------------------------------------------------------------------


@router.get("/closures", response_model=list[ExceptionalClosureResponse])
async def list_closures(
    current_user: dict = Depends(require_role("admin")),
) -> list[ExceptionalClosureResponse]:
    """Retourne toutes les fermetures exceptionnelles triees par date.

    Args:
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        Liste de ExceptionalClosureResponse.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tenant_service.get_exceptional_closures(session)


@router.post("/closures", response_model=ExceptionalClosureResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_closure(
    request: Request,
    body: ExceptionalClosureCreate,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> ExceptionalClosureResponse:
    """Cree une fermeture exceptionnelle pour une date future.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        body: Donnees de la fermeture.
        current_user: Utilisateur admin injecte par dependance.

    Returns:
        ExceptionalClosureResponse creee.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await tenant_service.add_exceptional_closure(session, body)
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
    return result


@router.delete("/closures/{closure_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_closure(
    request: Request,
    closure_id: int,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> None:
    """Supprime une fermeture exceptionnelle par son identifiant.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        closure_id: Identifiant de la fermeture a supprimer.
        current_user: Utilisateur admin injecte par dependance.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await tenant_service.delete_exceptional_closure(session, closure_id)
    await arq_pool.delete(_tenant_status_cache_key(current_user["tenant_slug"]))
