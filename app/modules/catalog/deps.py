"""Resolution du CatalogProvider par tenant (P9)."""

from fastapi import Depends
from sqlalchemy import text

from app.core.database import get_public_session
from app.core.http.deps import get_current_user
from app.core.tenancy.integration_mode import IntegrationMode
from app.modules.catalog.exceptions import ReadOnlyCatalogError
from app.modules.catalog.ports import CatalogProvider
from app.modules.catalog.providers import HubCatalogProvider, LocalCatalogProvider


async def _load_integration_mode(tenant_slug: str) -> IntegrationMode:
    """Lit ``integration_mode`` du tenant depuis ``public.tenants``.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        ``IntegrationMode`` du tenant ; ``STANDALONE`` si le tenant est
        introuvable ou si la colonne est nulle (comportement par defaut de la
        colonne, cf. migration 0044_tenant_integration_mode_pos_connections).
    """
    async with get_public_session() as session:
        value = await session.scalar(
            text("SELECT integration_mode FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
    return IntegrationMode(value) if value else IntegrationMode.STANDALONE


async def _load_active_connection_id(tenant_slug: str) -> int | None:
    """Retourne l'id de la connexion POS active du tenant, ou None.

    Args:
        tenant_slug: Slug du tenant.

    Returns:
        ``public.pos_connections.id`` de la connexion active, ou None si
        aucune connexion active n'existe (cas limite : tenant CONNECTED sans
        connexion active, ex. juste apres une revocation).
    """
    async with get_public_session() as session:
        return await session.scalar(
            text(
                "SELECT pc.id FROM public.pos_connections pc "
                "JOIN public.tenants t ON t.id = pc.tenant_id "
                "WHERE t.slug = :slug AND pc.status = 'active'"
            ),
            {"slug": tenant_slug},
        )


async def get_catalog_provider(tenant_slug: str) -> CatalogProvider:
    """Resout l'implementation CatalogProvider a utiliser pour un tenant.

    Appelee directement par les routes avec un ``tenant_slug`` deja resolu
    (header ``X-Tenant-Slug`` pour les lectures publiques, ou
    ``current_user["tenant_slug"]`` pour les ecritures authentifiees) --
    volontairement pas un ``Depends(...)`` de route, pour ne pas ajouter de
    dependance d'authentification a la route de listing public.

    Args:
        tenant_slug: Slug du tenant courant.

    Returns:
        ``LocalCatalogProvider`` si le tenant est STANDALONE (par defaut),
        ``HubCatalogProvider`` si CONNECTED (avec l'id de sa connexion active,
        ou None si CONNECTED sans connexion active).
    """
    mode = await _load_integration_mode(tenant_slug)
    if mode == IntegrationMode.CONNECTED:
        connection_id = await _load_active_connection_id(tenant_slug)
        return HubCatalogProvider(connection_id)
    return LocalCatalogProvider()


async def require_catalog_writable(current_user: dict = Depends(get_current_user)) -> dict:
    """Bloque les ecritures catalogue pour un tenant CONNECTED.

    Args:
        current_user: Utilisateur courant, deja authentifie.

    Returns:
        Le meme dict ``current_user``, inchange, si le tenant est STANDALONE.

    Raises:
        ReadOnlyCatalogError: si le tenant est CONNECTED.
    """
    mode = await _load_integration_mode(current_user["tenant_slug"])
    if mode == IntegrationMode.CONNECTED:
        raise ReadOnlyCatalogError()
    return current_user
