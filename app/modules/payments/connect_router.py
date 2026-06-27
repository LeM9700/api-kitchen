"""Routes Stripe Connect — onboarding et gestion des comptes connectés.

Endpoints :
    POST /payments/connect/onboarding
        Initie ou reprend l'onboarding Stripe Connect pour le tenant courant.
        Crée le compte Express si absent, génère un AccountLink et retourne l'URL.

    GET /payments/connect/status
        Retourne le statut d'onboarding (details_submitted, charges_enabled).

    GET /payments/connect/dashboard
        Retourne un lien vers le dashboard Express Stripe du restaurant.
        [🔒] Le lien expire dans quelques secondes — redirection immédiate.

Accès :
    - admin : accès aux 3 routes (gestion du propre restaurant)
    - super-admin : accès aux 3 routes + query param ?tenant_slug= pour inspecter
      n'importe quel tenant

[🔒 SÉCURITÉ]
    Rate limit sur /onboarding : 5/minute pour éviter la création abusive de
    comptes Stripe (chaque appel peut créer un acct_xxx sur votre compte plateforme).
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.core.http.deps import get_current_user, require_role
from app.core.http.limiter import limiter
from app.modules.payments import connect_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas inline (spécifiques à Connect, pas réutilisés ailleurs)
# ---------------------------------------------------------------------------


class ConnectOnboardingRequest(BaseModel):
    """Payload pour initier ou reprendre l'onboarding Stripe Connect.

    Attributes:
        return_url: URL vers laquelle Stripe redirige après l'onboarding réussi.
        refresh_url: URL vers laquelle Stripe redirige si l'AccountLink expire.
    """

    return_url: str
    refresh_url: str


class ConnectOnboardingResponse(BaseModel):
    """Réponse après initiation de l'onboarding.

    Attributes:
        url: URL de l'AccountLink Stripe — à ouvrir immédiatement (expire dans ~5 min).
        stripe_account_id: Identifiant du compte Express (acct_xxx).
    """

    url: str
    stripe_account_id: str


class ConnectStatusResponse(BaseModel):
    """Statut d'onboarding Stripe Connect pour un tenant.

    Attributes:
        stripe_account_id: Identifiant du compte Express, ou None si non initié.
        details_submitted: True si le restaurant a complété le formulaire Stripe.
        payouts_enabled: True si les virements vers le restaurant sont activés.
        charges_enabled: True si le restaurant peut recevoir des paiements.
        onboarding_complete: True si details_submitted ET charges_enabled.
    """

    stripe_account_id: str | None
    details_submitted: bool
    payouts_enabled: bool
    charges_enabled: bool
    onboarding_complete: bool


class ConnectDashboardResponse(BaseModel):
    """Lien vers le dashboard Express Stripe.

    Attributes:
        url: URL du dashboard — expire dans quelques secondes, rediriger immédiatement.
    """

    url: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/onboarding", response_model=ConnectOnboardingResponse)
@limiter.limit("5/minute")
async def initiate_onboarding(
    request: Request,
    body: ConnectOnboardingRequest,
    current_user: dict = Depends(require_role("admin", "super-admin")),
) -> ConnectOnboardingResponse:
    """Initie ou reprend l'onboarding Stripe Connect pour le tenant.

    Si le restaurant n'a pas encore de compte Stripe Express, en crée un
    automatiquement. Génère ensuite un AccountLink (URL à usage unique, ~5 min)
    que le frontend doit ouvrir immédiatement.

    Si l'onboarding a déjà été initié mais non terminé, génère un nouveau lien
    pour permettre la reprise (idempotent sur le compte, nouveau lien à chaque fois).

    Args:
        request: Requête FastAPI (requis par SlowAPI).
        body: return_url et refresh_url fournis par le frontend.
        current_user: Utilisateur admin ou super-admin authentifié.

    Returns:
        ConnectOnboardingResponse avec l'URL et le stripe_account_id.
    """
    tenant_slug = current_user["tenant_slug"]
    admin_email = current_user.get("email")

    account_id = await connect_service.get_or_create_connect_account(
        tenant_slug,
        admin_email=admin_email,
    )
    onboarding_url = await connect_service.create_account_link(
        account_id,
        return_url=body.return_url,
        refresh_url=body.refresh_url,
    )

    logger.info(
        "Connect onboarding link generated: tenant=%s account=%s",
        tenant_slug,
        account_id,
    )
    return ConnectOnboardingResponse(url=onboarding_url, stripe_account_id=account_id)


@router.get("/status", response_model=ConnectStatusResponse)
async def get_status(
    current_user: dict = Depends(require_role("admin", "super-admin")),
    tenant_slug: str | None = Query(
        None,
        description="Slug du tenant (super-admin uniquement, pour inspecter un autre restaurant)",
    ),
) -> ConnectStatusResponse:
    """Retourne le statut d'onboarding Stripe Connect pour le tenant.

    Interroge l'API Stripe en temps réel (pas de cache).

    Un super-admin peut passer ?tenant_slug=xxx pour vérifier n'importe quel
    restaurant. Un admin ne peut vérifier que son propre restaurant.

    Args:
        current_user: Utilisateur admin ou super-admin authentifié.
        tenant_slug: Slug optionnel (super-admin seulement).

    Returns:
        ConnectStatusResponse avec les flags Stripe.
    """
    # [🔒 SÉCURITÉ] Un admin ne peut inspecter que son propre tenant.
    # Seul le super-admin peut spécifier un tenant_slug différent.
    if tenant_slug and current_user.get("role") != "super-admin":
        from app.core.http.errors import AppError
        raise AppError("FORBIDDEN", "Accès refusé", 403)

    effective_slug = tenant_slug or current_user["tenant_slug"]
    status = await connect_service.get_connect_status(effective_slug)
    return ConnectStatusResponse(**status)


@router.get("/dashboard", response_model=ConnectDashboardResponse)
@limiter.limit("10/minute")
async def get_dashboard(
    request: Request,
    current_user: dict = Depends(require_role("admin", "super-admin")),
    tenant_slug: str | None = Query(
        None,
        description="Slug du tenant (super-admin uniquement)",
    ),
) -> ConnectDashboardResponse:
    """Génère un lien vers le dashboard Express Stripe du restaurant.

    [🔒 SÉCURITÉ] Le LoginLink expire dans quelques secondes.
    Le frontend doit rediriger immédiatement sans stocker l'URL.

    L'onboarding doit être terminé (details_submitted=True) pour accéder
    au dashboard. Retourne STRIPE_CONNECT_INCOMPLETE sinon.

    Args:
        request: Requête FastAPI (requis par SlowAPI).
        current_user: Utilisateur admin ou super-admin authentifié.
        tenant_slug: Slug optionnel (super-admin seulement).

    Returns:
        ConnectDashboardResponse avec l'URL du dashboard Express.
    """
    if tenant_slug and current_user.get("role") != "super-admin":
        from app.core.http.errors import AppError
        raise AppError("FORBIDDEN", "Accès refusé", 403)

    effective_slug = tenant_slug or current_user["tenant_slug"]
    url = await connect_service.get_dashboard_link(effective_slug)

    logger.info(
        "Connect dashboard link generated: tenant=%s by=%s",
        effective_slug,
        current_user.get("id"),
    )
    return ConnectDashboardResponse(url=url)
