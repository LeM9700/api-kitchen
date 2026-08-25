"""Mapping du payload brut du hub HubRise vers le format pivot NormalizedCatalogProduct.

Format reel confirme (https://www.hubrise.com/developers/api/catalogs) :
    {"id", "location_id", "data": {"products": [...], "categories": [...], "skus"
    n'existent pas au niveau racine -- chaque produit porte son propre "skus": [...]}}

[LIMITE CONNUE] NormalizedCatalogProduct est un format pivot PLAT (un prix, un
taux de TVA par produit) alors que HubRise modelise Produit -> SKUs[] (un
produit peut avoir plusieurs variantes/tailles, chacune avec son propre prix).
Ce mapping ne retient que la PREMIERE SKU de chaque produit -- un vrai support
multi-variantes necessiterait d'etendre NormalizedCatalogProduct et
_materialize_products (worker/tasks/catalog_sync.py) pour ecrire dans
ProductVariant, pas seulement Product. Pas fait ici : a traiter separement si
des produits multi-tailles sont effectivement utilises.

tax_rate HubRise est ventile par canal ({"delivery", "collection", "eat_in"}),
pas un flottant unique -- on retient eat_in en priorite (prix menu de
reference), puis delivery, puis collection.

Disponibilite : HubRise n'a pas de flag "disabled"/"deleted" au niveau produit
-- c'est ``skus[].restrictions.enabled`` (bool, defaut true, omis si true) qui
porte cette information, au niveau SKU.
"""

from app.modules.catalog.schemas import NormalizedCatalogProduct


class MalformedHubCatalogPayloadError(ValueError):
    """Le payload retourne par le hub POS ne respecte pas le format pivot attendu."""


def _resolve_tax_rate(raw_tax_rate) -> float | None:
    if raw_tax_rate is None:
        return None
    if isinstance(raw_tax_rate, (int, float)):
        return float(raw_tax_rate)
    if isinstance(raw_tax_rate, dict):
        for channel in ("eat_in", "delivery", "collection"):
            if raw_tax_rate.get(channel) is not None:
                return float(raw_tax_rate[channel])
        return None
    raise TypeError(f"Format de tax_rate inattendu: {type(raw_tax_rate)!r}")


def normalize_catalog(payload: dict) -> list[NormalizedCatalogProduct]:
    """Convertit le payload brut HubRise (``GET /catalogs/:id``) en liste de
    produits au format pivot.

    Args:
        payload: Reponse JSON brute du hub (``HubCatalogClient.fetch_catalog``).

    Returns:
        Liste de ``NormalizedCatalogProduct``, dans l'ordre du payload source.
        Les produits sans aucune SKU (donc sans prix determinable) sont ignores.

    Raises:
        MalformedHubCatalogPayloadError: si ``payload["data"]["products"]`` est
            absent, n'est pas une liste, ou si un element ne peut pas etre
            mappe (champ requis absent ou de type incorrect).
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MalformedHubCatalogPayloadError("Le champ 'data' est absent ou n'est pas un objet.")

    products = data.get("products")
    if not isinstance(products, list):
        raise MalformedHubCatalogPayloadError(
            "Le champ 'data.products' est absent ou n'est pas une liste."
        )

    categories_by_id = {
        str(c["id"]): c.get("name")
        for c in data.get("categories", [])
        if isinstance(c, dict) and "id" in c
    }

    normalized: list[NormalizedCatalogProduct] = []
    for index, raw in enumerate(products):
        try:
            skus = raw.get("skus") or []
            if not skus:
                # Produit sans SKU : aucun prix determinable, on l'ignore
                # plutot que d'inventer un prix a 0.
                continue
            first_sku = skus[0]

            category_id = raw.get("category_id")

            normalized.append(
                NormalizedCatalogProduct(
                    external_id=str(raw["id"]),
                    name=raw["name"],
                    description=raw.get("description"),
                    category=categories_by_id.get(str(category_id)) if category_id else None,
                    price=float(first_sku["price"]),
                    tax_rate=_resolve_tax_rate(raw.get("tax_rate")),
                    # [NON CONFIRME] Resolution image_ids -> URL non verifiee
                    # aupres de la Images API HubRise -- laissee vide plutot
                    # que de deviner un format d'URL.
                    image_url=None,
                    is_active=(first_sku.get("restrictions") or {}).get("enabled", True),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedHubCatalogPayloadError(
                f"Produit hub invalide a l'index {index}: {exc}"
            ) from exc
    return normalized
