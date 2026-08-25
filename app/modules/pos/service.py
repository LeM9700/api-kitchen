"""Service de connexion OAuth 2.0 a un hub POS externe.

Flux :
    1. POST /pos/connect/start -- genere un state a usage unique (stocke
       dans Redis, TTL 10 min), retourne l'URL d'autorisation du hub.
    2. Le restaurant autorise sur le site du hub POS.
    3. GET /pos/connect/callback -- consomme le state, echange le code
       contre des tokens, chiffre et persiste la connexion.
    4. POST /pos/connect/disconnect -- revoque la connexion (best-effort
       cote fournisseur) et repasse le tenant en mode standalone.

[SECURITE]
    - Le state est a usage unique : consume_oauth_state fait un GETDEL
      atomique (lecture + suppression en un seul aller-retour Redis), donc
      un rejeu echoue toujours (la cle n'existe plus).
    - Les tokens sont chiffres (app.core.services.crypto) avant toute
      ecriture en base, et jamais loggues.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_public_session
from app.core.http.errors import AppError
from app.core.services.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "pos_oauth_state:"


def is_configured() -> bool:
    """Retourne True si tous les settings necessaires au flux OAuth POS sont renseignes.

    Utilise pour desactiver explicitement /start et /callback (503) plutot que
    de laisser le flux echouer silencieusement avec une URL vide/cassee quand
    aucun fournisseur POS reel n'est encore configure.
    """
    return bool(
        settings.pos_hub_client_id
        and settings.pos_hub_authorize_url
        and settings.pos_hub_token_url
        and settings.pos_hub_redirect_uri
        and settings.pos_token_encryption_key
        and settings.pos_oauth_frontend_return_url
    )


def generate_state() -> str:
    """Genere un identifiant de state OAuth imprevisible.

    Returns:
        Chaine urlsafe de 43 caracteres (32 octets d'entropie).
    """
    return secrets.token_urlsafe(32)


async def store_oauth_state(redis, state: str, tenant_slug: str) -> None:
    """Associe un state OAuth au tenant qui a initie le flux, avec TTL.

    Args:
        redis: Pool Redis partage (ArqRedis, app.state.arq_pool).
        state: Identifiant genere par generate_state().
        tenant_slug: Slug du tenant qui a initie le flux.
    """
    await redis.setex(f"{_STATE_KEY_PREFIX}{state}", STATE_TTL_SECONDS, tenant_slug)


async def consume_oauth_state(redis, state: str) -> str:
    """Consomme un state OAuth a usage unique et retourne le tenant associe.

    Args:
        redis: Pool Redis partage.
        state: State recu sur le callback OAuth.

    Returns:
        Slug du tenant qui avait initie le flux.

    Raises:
        AppError: POS_OAUTH_INVALID_STATE (400) si le state est absent,
            expire ou deja consomme -- ces trois cas sont indistinguables
            volontairement (pas d'information donnee a un attaquant).
    """
    value = await redis.getdel(f"{_STATE_KEY_PREFIX}{state}")
    if value is None:
        raise AppError(
            "POS_OAUTH_INVALID_STATE",
            "State OAuth invalide, expire ou deja utilise.",
            400,
        )
    return value.decode() if isinstance(value, bytes) else value


def build_authorization_url(state: str) -> str:
    """Construit l'URL d'autorisation du hub POS pour un state donne.

    Args:
        state: Identifiant genere par generate_state() et deja stocke via
            store_oauth_state().

    Returns:
        URL complete a ouvrir cote frontend.
    """
    params = {
        "client_id": settings.pos_hub_client_id,
        "redirect_uri": settings.pos_hub_redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if settings.pos_hub_default_scopes:
        params["scope"] = settings.pos_hub_default_scopes
    return f"{settings.pos_hub_authorize_url}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    """Echange un code d'autorisation contre des tokens aupres du hub POS.

    [HubRise] Le client_id/client_secret sont passes en HTTP Basic Auth sur
    POST /oauth2/v1/token, pas dans le corps du formulaire -- voir
    https://www.hubrise.com/developers/api/authentication.

    Args:
        code: Code d'autorisation recu sur le callback OAuth.

    Returns:
        Dictionnaire avec access_token, refresh_token (ou None), expires_in
        (ou None), external_establishment_id, scope (ou None).

    Raises:
        AppError: POS_OAUTH_EXCHANGE_FAILED (502) si l'appel HTTP echoue, si
            le JSON est invalide, ou si access_token/establishment_id sont
            absents de la reponse.
    """
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.pos_hub_redirect_uri,
    }
    try:
        async with httpx.AsyncClient(
            auth=(settings.pos_hub_client_id, settings.pos_hub_client_secret)
        ) as client:
            response = await client.post(settings.pos_hub_token_url, data=payload, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("POS OAuth token exchange failed: status=%s", status)
        raise AppError(
            "POS_OAUTH_EXCHANGE_FAILED",
            "Echec de l'echange du code d'autorisation aupres du hub POS.",
            502,
        ) from exc

    access_token = data.get("access_token")
    establishment_id = data.get(settings.pos_hub_establishment_id_field)
    if not access_token or not establishment_id:
        logger.error("POS OAuth token response missing required fields")
        raise AppError(
            "POS_OAUTH_EXCHANGE_FAILED",
            "Reponse du hub POS incomplete.",
            502,
        )

    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "external_establishment_id": str(establishment_id),
        "scope": data.get("scope"),
    }


async def get_active_connection(tenant_slug: str) -> dict | None:
    """Retourne la connexion POS active du tenant, ou None.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        Mapping avec id, access_token_encrypted, refresh_token_encrypted,
        ou None si aucune connexion active.
    """
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT pc.id, pc.access_token_encrypted, pc.refresh_token_encrypted "
                "FROM public.pos_connections pc "
                "JOIN public.tenants t ON t.id = pc.tenant_id "
                "WHERE t.slug = :slug AND pc.status = 'active'"
            ),
            {"slug": tenant_slug},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def register_hub_callback(connection_id: int, access_token: str) -> None:
    """Enregistre l'URL de callback HubRise pour cette connexion.

    [HubRise] Un callback est specifique a une connexion (une seule URL par
    connexion, portee par le token utilise pour l'enregistrer) -- voir
    https://www.hubrise.com/developers/api/callbacks. Le payload webhook ne
    contenant aucun identifiant de location, c'est cette URL par connexion
    (connection_id dans le chemin) qui permet a /pos/catalog-webhook/{id} de
    savoir a quel tenant l'evenement appartient.

    Best-effort : un echec est logue mais ne bloque jamais l'activation de la
    connexion -- sync_stale_catalog_connections (cron) reste un filet de
    securite qui resynchronise meme sans callback actif.

    Args:
        connection_id: Id de la ligne public.pos_connections fraichement creee.
        access_token: Token OAuth en clair de cette connexion (non chiffre).
    """
    target_url = f"{settings.app_base_url}/api/v1/pos/catalog-webhook/{connection_id}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.pos_hub_api_base_url}/callback",
                json={"url": target_url, "events": {"catalog": "update"}},
                headers={"X-Access-Token": access_token},
                timeout=10.0,
            )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning(
            "POS hub callback registration failed: connection_id=%s", connection_id, exc_info=True
        )


async def save_connection(tenant_slug: str, token_data: dict) -> None:
    """Chiffre les tokens, upsert la connexion POS active pour ce tenant, et
    enregistre le callback HubRise correspondant.

    Passe egalement public.tenants.integration_mode a 'connected'.

    Args:
        tenant_slug: Slug du tenant qui a complete le flux OAuth.
        token_data: Dictionnaire retourne par exchange_code_for_tokens().

    Raises:
        AppError: si le tenant est introuvable (404) -- ne devrait pas
            arriver en usage normal (state deja valide le tenant), garde-fou
            defensif.
    """
    expires_at = None
    if token_data.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_data["expires_in"]))

    scope = token_data.get("scope")
    scopes_json = json.dumps(scope.split()) if scope else None

    async with get_public_session() as session:
        tenant_result = await session.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        tenant_id = tenant_result.scalar_one_or_none()
        if tenant_id is None:
            raise AppError("POS_OAUTH_EXCHANGE_FAILED", "Tenant introuvable.", 404)

        connection_result = await session.execute(
            text(
                "INSERT INTO public.pos_connections "
                "(tenant_id, provider, external_establishment_id, access_token_encrypted, "
                " refresh_token_encrypted, scopes, status, connected_at, token_expires_at) "
                "VALUES (:tenant_id, :provider, :external_establishment_id, :access_token_encrypted, "
                " :refresh_token_encrypted, :scopes, 'active', now(), :token_expires_at) "
                # [LIMITE CONNUE] voir RUNBOOK.md section 7 -- pas de verification cross-tenant sur ce conflit.
                "ON CONFLICT (provider, external_establishment_id) DO UPDATE SET "
                " tenant_id = EXCLUDED.tenant_id, "
                " access_token_encrypted = EXCLUDED.access_token_encrypted, "
                " refresh_token_encrypted = EXCLUDED.refresh_token_encrypted, "
                " scopes = EXCLUDED.scopes, "
                " status = 'active', "
                " connected_at = now(), "
                " token_expires_at = EXCLUDED.token_expires_at "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "provider": settings.pos_hub_provider_name,
                "external_establishment_id": token_data["external_establishment_id"],
                "access_token_encrypted": encrypt_secret(token_data["access_token"]),
                "refresh_token_encrypted": (
                    encrypt_secret(token_data["refresh_token"]) if token_data.get("refresh_token") else None
                ),
                "scopes": scopes_json,
                "token_expires_at": expires_at,
            },
        )
        connection_id = connection_result.scalar_one()
        await session.execute(
            text("UPDATE public.tenants SET integration_mode = 'connected' WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        await session.commit()

    await register_hub_callback(connection_id, token_data["access_token"])


async def disconnect(tenant_slug: str) -> None:
    """Revoque la connexion POS active du tenant.

    Appelle la revocation cote fournisseur en best-effort si
    settings.pos_hub_revoke_url est configure -- un echec de cet appel
    (panne du hub) ne bloque jamais la revocation locale.

    Args:
        tenant_slug: Slug du tenant.

    Raises:
        AppError: POS_NOT_CONNECTED (404) si aucune connexion active n'existe.
    """
    connection = await get_active_connection(tenant_slug)
    if connection is None:
        raise AppError("POS_NOT_CONNECTED", "Aucune connexion POS active pour ce restaurant.", 404)

    if settings.pos_hub_revoke_url:
        try:
            access_token = decrypt_secret(connection["access_token_encrypted"])
            async with httpx.AsyncClient() as client:
                await client.post(
                    settings.pos_hub_revoke_url,
                    data={
                        "token": access_token,
                        "client_id": settings.pos_hub_client_id,
                        "client_secret": settings.pos_hub_client_secret,
                    },
                    timeout=10.0,
                )
        except Exception:
            logger.warning("POS token revocation call failed: tenant=%s", tenant_slug, exc_info=True)

    async with get_public_session() as session:
        await session.execute(
            text(
                "UPDATE public.pos_connections "
                "SET status = 'revoked', access_token_encrypted = NULL, refresh_token_encrypted = NULL "
                "WHERE id = :id"
            ),
            {"id": connection["id"]},
        )
        await session.execute(
            text("UPDATE public.tenants SET integration_mode = 'standalone' WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        await session.commit()
