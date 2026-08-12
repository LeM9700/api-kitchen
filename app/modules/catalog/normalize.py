"""Mapping du payload brut du hub POS vers le format pivot NormalizedCatalogProduct.

[HYPOTHESE NON CONFIRMEE] Le format d'entree ({"products": [{"id", "name",
"price", "tax_rate", ...}]}) est une hypothese raisonnable, pas un contrat
confirme aupres du vrai fournisseur du hub. Ce fichier est le seul point a
modifier si le vrai format differe.
"""

from app.modules.catalog.schemas import NormalizedCatalogProduct


class MalformedHubCatalogPayloadError(ValueError):
    """Le payload retourne par le hub POS ne respecte pas le format pivot attendu."""


def normalize_catalog(payload: dict) -> list[NormalizedCatalogProduct]:
    """Convertit le payload brut du hub en liste de produits au format pivot.

    Args:
        payload: Reponse JSON brute du hub (``HubCatalogClient.fetch_catalog``).

    Returns:
        Liste de ``NormalizedCatalogProduct``, dans l'ordre du payload source.

    Raises:
        MalformedHubCatalogPayloadError: si ``payload["products"]`` est absent,
            n'est pas une liste, ou si un element ne peut pas etre mappe (champ
            requis absent ou de type incorrect).
    """
    products = payload.get("products")
    if not isinstance(products, list):
        raise MalformedHubCatalogPayloadError("Le champ 'products' est absent ou n'est pas une liste.")

    normalized: list[NormalizedCatalogProduct] = []
    for index, raw in enumerate(products):
        try:
            normalized.append(
                NormalizedCatalogProduct(
                    external_id=str(raw["id"]),
                    name=raw["name"],
                    description=raw.get("description"),
                    category=raw.get("category"),
                    price=float(raw["price"]),
                    tax_rate=float(raw["tax_rate"]) if raw.get("tax_rate") is not None else None,
                    image_url=raw.get("image_url"),
                    is_active=raw.get("is_active", True),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedHubCatalogPayloadError(
                f"Produit hub invalide a l'index {index}: {exc}"
            ) from exc
    return normalized
