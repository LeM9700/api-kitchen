"""Routes de connexion OAuth 2.0 a un hub POS externe.

Endpoints :
    POST /pos/connect/start
        Initie le flux OAuth pour le tenant courant. Genere un state a
        usage unique et retourne l'URL d'autorisation du hub POS.

    GET /pos/connect/callback
        Callback OAuth atteint par une navigation navigateur depuis le hub
        POS. Echange le code contre des tokens, persiste la connexion, puis
        redirige toujours le navigateur vers le frontend.

    POST /pos/connect/disconnect
        Revoque la connexion POS active du tenant courant.

Acces :
    - admin/super-admin : start et disconnect (action sensible touchant
      paiements/commandes).
    - callback : pas de dependance d'authentification (navigation navigateur
      sans header Bearer) -- protege intrinsequement par le state OAuth a
      usage unique.

[SECURITE]
    - Rate limit sur /start : 5/minute (empeche la generation abusive de
      states/URLs d'autorisation).
    - Le callback ne redirige jamais vers une URL fournie par le client --
      uniquement vers settings.pos_oauth_frontend_return_url (evite tout
      open-redirect).
    - Aucun token n'est jamais loggue ni renvoye dans une reponse API.
"""
import logging

from arq import ArqRedis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.config import settings
from app.core.http.deps import get_arq_pool, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.modules.pos import service as pos_service
from app.modules.pos import webhook_service

logger = logging.getLogger(__name__)

router = APIRouter()


class PosConnectStartResponse(BaseModel):
    """URL d'autorisation OAuth a ouvrir immediatement cote frontend."""

    url: str


class PosDisconnectResponse(BaseModel):
    """Confirmation de la revocation de la connexion POS."""

    status: str


@router.post("/start", response_model=PosConnectStartResponse)
@limiter.limit("5/minute")
async def start_connection(
    request: Request,
    current_user: dict = Depends(require_role("admin", "super-admin")),
    redis: ArqRedis = Depends(get_arq_pool),
) -> PosConnectStartResponse:
    """Initie le flux OAuth POS pour le tenant courant.

    Args:
        request: Requete FastAPI (requis par SlowAPI).
        current_user: Utilisateur admin ou super-admin authentifie.
        redis: Pool Redis partage, utilise pour stocker le state OAuth.

    Returns:
        PosConnectStartResponse avec l'URL d'autorisation du hub POS.

    Raises:
        AppError: POS_ALREADY_CONNECTED (409) si une connexion active existe deja.
    """
    tenant_slug = current_user["tenant_slug"]

    if not pos_service.is_configured():
        raise AppError(
            "POS_NOT_CONFIGURED",
            "La connexion POS n'est pas configuree sur ce serveur.",
            503,
        )

    if await pos_service.get_active_connection(tenant_slug) is not None:
        raise AppError(
            "POS_ALREADY_CONNECTED",
            "Une connexion POS est deja active pour ce restaurant.",
            409,
        )

    state = pos_service.generate_state()
    await pos_service.store_oauth_state(redis, state, tenant_slug)
    url = pos_service.build_authorization_url(state)

    logger.info("POS OAuth flow started: tenant=%s", tenant_slug)
    return PosConnectStartResponse(url=url)


@router.get("/callback")
async def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    redis: ArqRedis = Depends(get_arq_pool),
) -> RedirectResponse:
    """Callback OAuth atteint par le navigateur depuis le hub POS.

    Consomme le state (usage unique), echange le code contre des tokens,
    persiste la connexion, puis redirige toujours vers
    settings.pos_oauth_frontend_return_url -- jamais vers une URL fournie
    par le client.

    Args:
        code: Code d'autorisation renvoye par le hub POS.
        state: State OAuth genere par /start.
        error: Code d'erreur renvoye par le hub si l'utilisateur refuse
            l'autorisation.
        redis: Pool Redis partage, utilise pour consommer le state OAuth.

    Returns:
        RedirectResponse vers le frontend avec ?status=success ou
        ?status=error&reason=....
    """
    return_url = settings.pos_oauth_frontend_return_url

    if not pos_service.is_configured():
        raise AppError(
            "POS_NOT_CONFIGURED",
            "La connexion POS n'est pas configuree sur ce serveur.",
            503,
        )

    if error or not code or not state:
        return RedirectResponse(f"{return_url}?status=error&reason=denied")

    try:
        tenant_slug = await pos_service.consume_oauth_state(redis, state)
    except AppError:
        return RedirectResponse(f"{return_url}?status=error&reason=invalid_state")

    try:
        token_data = await pos_service.exchange_code_for_tokens(code)
        await pos_service.save_connection(tenant_slug, token_data)
    except AppError:
        return RedirectResponse(f"{return_url}?status=error&reason=exchange_failed")
    except Exception:
        logger.exception("POS callback failed unexpectedly: tenant=%s", tenant_slug)
        return RedirectResponse(f"{return_url}?status=error&reason=internal_error")

    logger.info("POS connection activated: tenant=%s", tenant_slug)
    return RedirectResponse(f"{return_url}?status=success")


@router.post("/disconnect", response_model=PosDisconnectResponse)
async def disconnect_connection(
    current_user: dict = Depends(require_role("admin", "super-admin")),
) -> PosDisconnectResponse:
    """Revoque la connexion POS active du tenant courant.

    Args:
        current_user: Utilisateur admin ou super-admin authentifie.

    Returns:
        PosDisconnectResponse avec status="revoked".

    Raises:
        AppError: POS_NOT_CONNECTED (404) si aucune connexion active n'existe.
    """
    tenant_slug = current_user["tenant_slug"]
    await pos_service.disconnect(tenant_slug)
    logger.info("POS connection revoked: tenant=%s by=%s", tenant_slug, current_user.get("id"))
    return PosDisconnectResponse(status="revoked")


webhook_router = APIRouter()


class PosCatalogWebhookResponse(BaseModel):
    """Confirmation de prise en compte du webhook (jamais de traitement synchrone)."""

    accepted: bool


@webhook_router.post(
    "/catalog-webhook/{connection_id}", response_model=PosCatalogWebhookResponse, status_code=202
)
async def catalog_webhook(connection_id: int, request: Request) -> PosCatalogWebhookResponse:
    """Webhook entrant HubRise : notifie un changement d'inventaire cote caisse.

    [HubRise] Un callback est enregistre PAR CONNEXION (voir
    ``pos_service.register_hub_callback``), avec ``connection_id`` dans le
    chemin -- le payload lui-meme ne porte aucun identifiant de location, donc
    c'est cette URL qui identifie le tenant, pas le corps de la requete.

    Ne fait jamais d'appel synchrone au hub -- se contente d'enqueue
    sync_catalog_from_hub et retourne immediatement.

    [NOTE] Le pool arq (``get_arq_pool``) est recupere manuellement, apres
    toutes les verifications, plutot que via ``Depends`` -- une dependance
    FastAPI est resolue avant le corps de la fonction, ce qui casserait le
    503/401/404 attendu sur une requete non configuree/non signee (acces
    a ``app.state.arq_pool`` avant meme la verification de signature).

    Args:
        connection_id: Id de connexion POS, tel qu'embarque dans l'URL de
            callback enregistree pour cette connexion.
        request: Requete FastAPI (corps brut lu pour la verification de signature).

    Returns:
        PosCatalogWebhookResponse(accepted=True) si le job a ete enqueue.

    Raises:
        AppError: POS_WEBHOOK_NOT_CONFIGURED (503) si non configure.
        AppError: POS_WEBHOOK_INVALID_SIGNATURE (401) si la signature est invalide.
        AppError: POS_WEBHOOK_UNKNOWN_CONNECTION (404) si la connexion est inconnue/inactive.
    """
    if not webhook_service.is_webhook_configured():
        raise AppError("POS_WEBHOOK_NOT_CONFIGURED", "Le webhook catalogue POS n'est pas configure.", 503)

    raw_body = await request.body()
    signature = request.headers.get(webhook_service.WEBHOOK_SIGNATURE_HEADER)
    if not webhook_service.verify_signature(raw_body, signature):
        raise AppError("POS_WEBHOOK_INVALID_SIGNATURE", "Signature webhook invalide.", 401)

    if not await webhook_service.is_connection_active(connection_id):
        raise AppError("POS_WEBHOOK_UNKNOWN_CONNECTION", "Connexion POS inconnue ou inactive.", 404)

    redis: ArqRedis = get_arq_pool(request)
    await redis.enqueue_job("sync_catalog_from_hub", connection_id=connection_id)
    logger.info("POS catalog webhook accepted: connection_id=%s", connection_id)
    return PosCatalogWebhookResponse(accepted=True)
