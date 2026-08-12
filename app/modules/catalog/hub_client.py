"""Client HTTP pour recuperer le catalogue depuis le hub POS.

[HYPOTHESE NON CONFIRMEE] Le format de l'appel (GET, Authorization: Bearer
<access_token dechiffre>) est une hypothese placeholder -- a confirmer avec le
vrai fournisseur du hub. Isole ici pour qu'un vrai contrat ne necessite de
modifier que ce fichier.
"""
from typing import Protocol

import httpx

from app.core.config import settings
from app.core.services import crypto


class HubCatalogClient(Protocol):
    async def fetch_catalog(self, connection: dict) -> dict: ...


class HubCatalogClientNotConfigured(RuntimeError):
    """Levee quand pos_hub_catalog_url est vide (fonctionnalite desactivee)."""


def is_configured() -> bool:
    """Retourne True si l'URL du catalogue hub est configuree."""
    return bool(settings.pos_hub_catalog_url)


class HttpHubCatalogClient:
    """Implementation HTTP du port ``HubCatalogClient``."""

    async def fetch_catalog(self, connection: dict) -> dict:
        """Recupere le catalogue brut du hub pour une connexion POS active.

        Args:
            connection: Mapping avec au moins ``access_token_encrypted``
                (tel que retourne par la requete SQL sur ``public.pos_connections``).

        Returns:
            Reponse JSON brute du hub.

        Raises:
            HubCatalogClientNotConfigured: si ``pos_hub_catalog_url`` est vide.
            httpx.HTTPError: si l'appel HTTP echoue.
        """
        if not is_configured():
            raise HubCatalogClientNotConfigured(
                "POS_HUB_CATALOG_URL n'est pas configure -- impossible de "
                "recuperer le catalogue du hub."
            )
        access_token = crypto.decrypt_secret(connection["access_token_encrypted"])
        async with httpx.AsyncClient() as client:
            response = await client.get(
                settings.pos_hub_catalog_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15.0,
            )
        response.raise_for_status()
        return response.json()
