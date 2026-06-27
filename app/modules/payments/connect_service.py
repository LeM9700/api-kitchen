"""Service Stripe Connect — onboarding des tenants restaurants.

Architecture :
    Chaque tenant possède un compte Stripe Express (acct_xxx) stocké dans
    public.tenant_configs.stripe_account_id. Les paiements clients sont créés
    directement sur ce compte connecté (Stripe Direct Charges). La plateforme
    (super-admin) utilise ses propres clés pour orchestrer les opérations au
    nom des restaurants, sans jamais stocker les secrets du restaurant.

Flow d'onboarding :
    1. POST /payments/connect/onboarding
       → Crée le compte Express si absent, génère un AccountLink, retourne l'URL.
    2. Le restaurant complète l'onboarding sur le site Stripe.
    3. Stripe redirige vers return_url (fourni par le frontend).
    4. GET /payments/connect/status → vérifie details_submitted + charges_enabled.
    5. GET /payments/connect/dashboard → lien vers le dashboard Express du restaurant.

[🔒 SÉCURITÉ]
    - Aucun secret du restaurant n'est stocké côté plateforme.
    - L'AccountLink expire après quelques minutes — jamais mis en cache.
    - Le LoginLink (dashboard Express) expire après quelques secondes — redirection immédiate.
    - L'accès aux routes est limité aux rôles admin et super-admin.
"""
import logging

import anyio
import stripe
from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_public_session
from app.core.http.errors import AppError

stripe.api_key = settings.stripe_secret_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------


async def _get_stripe_account_id(tenant_slug: str) -> str | None:
    """Retourne le stripe_account_id stocké en base pour ce tenant, ou None.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        Identifiant de compte Stripe Express (acct_xxx) ou None.
    """
    async with get_public_session() as session:
        row = await session.execute(
            text(
                "SELECT tc.stripe_account_id "
                "FROM public.tenant_configs tc "
                "JOIN public.tenants t ON t.id = tc.tenant_id "
                "WHERE t.slug = :slug"
            ),
            {"slug": tenant_slug},
        )
        result = row.scalar_one_or_none()
        return str(result) if result else None


async def _save_stripe_account_id(tenant_slug: str, account_id: str) -> None:
    """Persiste le stripe_account_id dans public.tenant_configs.

    Args:
        tenant_slug: Slug du tenant.
        account_id: Identifiant Stripe Express à stocker (acct_xxx).
    """
    async with get_public_session() as session:
        await session.execute(
            text(
                "UPDATE public.tenant_configs tc "
                "SET stripe_account_id = :account_id "
                "FROM public.tenants t "
                "WHERE t.id = tc.tenant_id AND t.slug = :slug"
            ),
            {"account_id": account_id, "slug": tenant_slug},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Service public
# ---------------------------------------------------------------------------


async def get_or_create_connect_account(
    tenant_slug: str,
    admin_email: str | None = None,
) -> str:
    """Récupère le compte Stripe Express existant ou en crée un nouveau.

    Si le compte n'existe pas encore, crée un compte Express Stripe avec les
    capacités card_payments et transfers, puis le persiste en base.

    Args:
        tenant_slug: Slug du tenant restaurant.
        admin_email: Email de l'admin, transmis à Stripe pour pré-remplir le formulaire.

    Returns:
        Identifiant du compte Stripe Express (acct_xxx).

    Raises:
        AppError: STRIPE_CONNECT_ERROR si la création du compte échoue.
    """
    account_id = await _get_stripe_account_id(tenant_slug)
    if account_id:
        return account_id

    # Créer un nouveau compte Express
    params: dict = {
        "type": "express",
        "country": "FR",
        "capabilities": {
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
    }
    if admin_email:
        params["email"] = admin_email

    try:
        account = await anyio.to_thread.run_sync(
            lambda: stripe.Account.create(**params)
        )
    except stripe.error.StripeError as exc:
        logger.error("stripe.Account.create failed for tenant=%s: %s", tenant_slug, exc)
        raise AppError(
            "STRIPE_CONNECT_ERROR",
            "Impossible de créer le compte Stripe. Veuillez réessayer.",
            502,
        ) from exc

    await _save_stripe_account_id(tenant_slug, account.id)
    logger.info("Stripe Express account created: tenant=%s account=%s", tenant_slug, account.id)
    return account.id


async def create_account_link(
    account_id: str,
    return_url: str,
    refresh_url: str,
) -> str:
    """Génère un AccountLink Stripe pour l'onboarding ou la reprise.

    Le lien est à usage unique et expire après quelques minutes.

    Args:
        account_id: Identifiant du compte Express (acct_xxx).
        return_url: URL vers laquelle Stripe redirige après l'onboarding.
        refresh_url: URL vers laquelle Stripe redirige si le lien expire.

    Returns:
        URL de l'AccountLink à utiliser immédiatement.

    Raises:
        AppError: STRIPE_CONNECT_ERROR si la génération du lien échoue.
    """
    try:
        link = await anyio.to_thread.run_sync(
            lambda: stripe.AccountLink.create(
                account=account_id,
                return_url=return_url,
                refresh_url=refresh_url,
                type="account_onboarding",
            )
        )
    except stripe.error.StripeError as exc:
        logger.error("stripe.AccountLink.create failed for account=%s: %s", account_id, exc)
        raise AppError(
            "STRIPE_CONNECT_ERROR",
            "Impossible de générer le lien d'onboarding Stripe. Veuillez réessayer.",
            502,
        ) from exc

    return link.url


async def get_connect_status(tenant_slug: str) -> dict:
    """Retourne le statut d'onboarding Stripe Connect pour un tenant.

    Interroge l'API Stripe en temps réel pour avoir l'état le plus récent.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        Dictionnaire avec stripe_account_id, details_submitted, payouts_enabled,
        charges_enabled et onboarding_complete.

    Raises:
        AppError: STRIPE_CONNECT_NOT_STARTED si aucun compte n'existe.
        AppError: STRIPE_CONNECT_ERROR si l'appel Stripe échoue.
    """
    account_id = await _get_stripe_account_id(tenant_slug)
    if not account_id:
        return {
            "stripe_account_id": None,
            "details_submitted": False,
            "payouts_enabled": False,
            "charges_enabled": False,
            "onboarding_complete": False,
        }

    try:
        account = await anyio.to_thread.run_sync(
            lambda: stripe.Account.retrieve(account_id)
        )
    except stripe.error.StripeError as exc:
        logger.error("stripe.Account.retrieve failed for account=%s: %s", account_id, exc)
        raise AppError(
            "STRIPE_CONNECT_ERROR",
            "Impossible de vérifier le statut Stripe. Veuillez réessayer.",
            502,
        ) from exc

    return {
        "stripe_account_id": account_id,
        "details_submitted": account.details_submitted,
        "payouts_enabled": account.payouts_enabled,
        "charges_enabled": account.charges_enabled,
        "onboarding_complete": account.details_submitted and account.charges_enabled,
    }


async def get_dashboard_link(tenant_slug: str) -> str:
    """Génère un lien vers le dashboard Express Stripe du restaurant.

    [🔒 SÉCURITÉ] Le LoginLink expire dans quelques secondes — le frontend doit
    rediriger immédiatement sans mettre l'URL en cache.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        URL du dashboard Express Stripe.

    Raises:
        AppError: STRIPE_CONNECT_NOT_STARTED si l'onboarding n'a pas commencé.
        AppError: STRIPE_CONNECT_INCOMPLETE si l'onboarding n'est pas terminé.
        AppError: STRIPE_CONNECT_ERROR si l'appel Stripe échoue.
    """
    account_id = await _get_stripe_account_id(tenant_slug)
    if not account_id:
        raise AppError(
            "STRIPE_CONNECT_NOT_STARTED",
            "L'onboarding Stripe n'a pas encore été initié pour ce restaurant.",
            409,
        )

    try:
        # Vérifier que l'onboarding est terminé avant de générer un lien dashboard
        account = await anyio.to_thread.run_sync(
            lambda: stripe.Account.retrieve(account_id)
        )
        if not account.details_submitted:
            raise AppError(
                "STRIPE_CONNECT_INCOMPLETE",
                "L'onboarding Stripe n'est pas terminé. Complétez d'abord l'inscription.",
                409,
            )

        link = await anyio.to_thread.run_sync(
            lambda: stripe.Account.create_login_link(account_id)
        )
    except AppError:
        raise
    except stripe.error.StripeError as exc:
        logger.error("stripe login link failed for account=%s: %s", account_id, exc)
        raise AppError(
            "STRIPE_CONNECT_ERROR",
            "Impossible de générer le lien vers le dashboard Stripe.",
            502,
        ) from exc

    return link.url
