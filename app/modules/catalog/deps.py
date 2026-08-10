"""Resolution du CatalogProvider par tenant (P9)."""

from sqlalchemy import text

from app.core.database import get_public_session
from app.core.tenancy.integration_mode import IntegrationMode
from app.modules.catalog.ports import CatalogProvider
from app.modules.catalog.providers import ConnectedCatalogProvider, LocalCatalogProvider


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
        ``ConnectedCatalogProvider`` si CONNECTED.
    """
    mode = await _load_integration_mode(tenant_slug)
    if mode == IntegrationMode.CONNECTED:
        return ConnectedCatalogProvider()
    return LocalCatalogProvider()
