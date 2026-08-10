"""Router FastAPI pour la gestion des allergènes réglementaires et des dietary tags."""

from fastapi import APIRouter, Depends, Request

from app.core.database import get_tenant_session
from app.core.http.deps import get_pagination, require_role
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.catalog.allergen import allergen_service
from app.modules.catalog.allergen.allergen_schemas import (
    AllergenChangeAuditResponse,
    AllergenDefinitionCreate,
    AllergenDefinitionResponse,
    DietaryTagResponse,
    IngredientAllergenSet,
    ProductAllergenPatch,
    ProductAllergenResponse,
    ProductAllergenSummary,
    ProductDietaryTagsSet,
)
from app.modules.catalog.deps import require_catalog_writable

router = APIRouter()


# ---------------------------------------------------------------------------
# Définitions d'allergènes
# ---------------------------------------------------------------------------


@router.get("/allergens", response_model=list[AllergenDefinitionResponse])
async def list_allergens(request: Request):
    """Liste tous les allergènes définis pour le tenant (14 EU + personnalisés).

    Route publique — aucune authentification requise.
    """
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await allergen_service.list_allergen_definitions(session)


@router.post("/allergens", response_model=AllergenDefinitionResponse, status_code=201)
async def create_allergen(
    body: AllergenDefinitionCreate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Crée un allergène personnalisé (non réglementaire).

    [🔒 SÉCURITÉ] Admin uniquement. is_regulatory est forcé à False.

    Args:
        body: Payload AllergenDefinitionCreate (name, slug, description optionnel).
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await allergen_service.create_custom_allergen(session, body)


# ---------------------------------------------------------------------------
# Ingrédients → allergènes
# ---------------------------------------------------------------------------


@router.get(
    "/ingredients/{ingredient_id}/allergens",
    response_model=list[IngredientAllergenSet],
)
async def get_ingredient_allergens(
    ingredient_id: int,
    current_user=Depends(require_role("staff", "admin")),
):
    """Retourne les allergènes associés à un ingrédient.

    Args:
        ingredient_id: Clé primaire de l'ingrédient.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        rows = await allergen_service.get_ingredient_allergens(session, ingredient_id)
        return [IngredientAllergenSet(allergen_id=r.allergen_id, level=r.level) for r in rows]


@router.put(
    "/ingredients/{ingredient_id}/allergens",
    response_model=list[IngredientAllergenSet],
)
async def set_ingredient_allergens(
    ingredient_id: int,
    body: list[IngredientAllergenSet],
    current_user=Depends(require_role("staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Remplace les allergènes d'un ingrédient et propage aux produits liés.

    [⚠️ PROD] Déclenche un recalcul sur tous les produits contenant cet ingrédient.

    Args:
        ingredient_id: Clé primaire de l'ingrédient.
        body: Liste complète des associations allergen_id + level à appliquer.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        rows = await allergen_service.set_ingredient_allergens(session, ingredient_id, body)
        return [IngredientAllergenSet(allergen_id=r.allergen_id, level=r.level) for r in rows]


# ---------------------------------------------------------------------------
# Produits → allergènes
# ---------------------------------------------------------------------------


@router.get("/products/{product_id}/allergens", response_model=ProductAllergenSummary)
async def get_product_allergens(product_id: int, request: Request):
    """Retourne le résumé allergènes d'un produit (merge ingredient + manuel).

    Route publique — aucune authentification requise.

    Args:
        product_id: Clé primaire du produit.
    """
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await allergen_service.get_product_allergen_summary(session, product_id)


@router.patch(
    "/products/{product_id}/allergens/{allergen_id}",
    response_model=ProductAllergenResponse,
)
async def patch_product_allergen(
    request: Request,
    product_id: int,
    allergen_id: int,
    body: ProductAllergenPatch,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Déclare manuellement un allergène sur un produit (priorité sur le calcul auto).

    [🔒 SÉCURITÉ] Génère un audit trail réglementaire immuable à chaque appel.

    Args:
        product_id: Clé primaire du produit.
        allergen_id: Clé primaire de l'allergène.
        body: Niveau déclaré (present / traces / absent) + raison optionnelle.
    """
    ip_address = request.client.host if request.client else None
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        pa = await allergen_service.set_product_allergen_manual(
            session,
            product_id,
            allergen_id,
            body.level,
            user_id=current_user["id"],
            ip_address=ip_address,
            reason=body.reason,
        )
        from app.modules.catalog.allergen.allergen_models import AllergenDefinition
        ad = await session.get(AllergenDefinition, allergen_id)
        return ProductAllergenResponse(
            allergen_id=pa.allergen_id,
            allergen_name=ad.name if ad else "",
            allergen_slug=ad.slug if ad else "",
            level=pa.level,
            source=pa.source,
            is_regulatory=ad.is_regulatory if ad else False,
        )


@router.post("/products/{product_id}/allergens/recompute", status_code=204)
@limiter.limit("10/minute")
async def recompute_product_allergens(
    request: Request,
    product_id: int,
    current_user=Depends(require_role("admin", "staff")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Force le recalcul des allergènes d'un produit depuis ses ingrédients.

    [⚠️ PROD] Ne supprime pas les déclarations manuelles (source='manual').
    [⚡ PERF] Rate-limité à 10 appels/minute pour prévenir les abus de recalcul.

    Args:
        product_id: Clé primaire du produit.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await allergen_service.recompute_product_allergens(session, product_id)


@router.get(
    "/products/{product_id}/allergens/audit",
    response_model=PaginatedResponse[AllergenChangeAuditResponse],
)
async def get_product_allergen_audit(
    product_id: int,
    pagination: PaginationParams = Depends(get_pagination),
    current_user=Depends(require_role("admin")),
):
    """Retourne l'historique paginé des changements d'allergènes d'un produit.

    [🔒 SÉCURITÉ] Admin uniquement — données de traçabilité réglementaire.
    Trié par date décroissante (le plus récent en premier).

    Args:
        product_id: Clé primaire du produit.
        pagination: Paramètres page / page_size.
    """
    offset = (pagination.page - 1) * pagination.page_size
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await allergen_service.get_product_allergen_audit(
            session,
            product_id,
            limit=pagination.page_size,
            offset=offset,
        )
    return PaginatedResponse.build(items, total, pagination)


# ---------------------------------------------------------------------------
# Dietary tags
# ---------------------------------------------------------------------------


@router.get("/dietary-tags", response_model=list[DietaryTagResponse])
async def list_dietary_tags(request: Request):
    """Liste tous les dietary tags disponibles dans le tenant.

    Route publique — aucune authentification requise.
    """
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await allergen_service.list_dietary_tags(session)


@router.put("/products/{product_id}/dietary-tags", response_model=list[DietaryTagResponse])
async def set_product_dietary_tags(
    product_id: int,
    body: ProductDietaryTagsSet,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Remplace les dietary tags d'un produit (PUT complet).

    Args:
        product_id: Clé primaire du produit.
        body: Liste des tag_ids à appliquer (liste vide = tout supprimer).
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await allergen_service.set_product_dietary_tags(session, product_id, body.tag_ids)
