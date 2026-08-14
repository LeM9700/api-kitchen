from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.core.database import get_tenant_session
from app.core.http.deps import get_arq_pool, get_pagination, require_permission, require_role
from app.core.http.errors import AppError
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginationParams
from app.core.services.cache import get_cached_json, invalidate_prefix, set_cached_json
from app.modules.catalog import override_repository, service
from app.modules.catalog.deps import get_catalog_provider, require_catalog_writable
from app.modules.catalog.models import Product
from app.modules.catalog.schemas import (
    CatalogCsvConfirmResponse,
    CatalogCsvDryRunResponse,
    CatalogCsvImportRequest,
    CatalogPaginatedResponse,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    ExtraCreate,
    ExtraOut,
    ExtraUpdate,
    PriceAuditOut,
    ProductAvailabilityOverrideCreate,
    ProductAvailabilityOverrideOut,
    ProductCreate,
    ProductDetailOut,
    ProductExtraLink,
    ProductOut,
    ProductOverrideCreate,
    ProductOverrideOut,
    ProductRecommendationCreate,
    ProductRecommendationOut,
    ProductRecommendationUpdate,
    ProductSummaryOut,
    ProductSuggestionOut,
    ProductUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)

router = APIRouter()


def _user_id(current_user: dict) -> int | None:
    value = current_user.get("id")
    return int(value) if value is not None else None


def _tenant_slug_from_header(request: Request) -> str:
    slug = request.headers.get("X-Tenant-Slug")
    if not slug:
        raise AppError("MISSING_TENANT_SLUG", "X-Tenant-Slug header is required", 400)
    return slug


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=CatalogPaginatedResponse[CategoryOut])
@limiter.limit("60/minute")
async def categories(
    request: Request,
    pagination: PaginationParams = Depends(get_pagination),
):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        items, total = await service.list_categories(session, pagination)
    return CatalogPaginatedResponse.build(items, total, pagination)


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryCreate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
    redis=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        category = await service.create_category(session, body)
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return category


@router.put("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: int,
    body: CategoryUpdate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
    redis=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        category = await service.update_category(session, category_id, body)
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return category


@router.get("/categories/{category_id}/products", response_model=list[ProductSummaryOut])
@limiter.limit("60/minute")
async def products_by_category(request: Request, category_id: int):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        items = await service.list_products_by_category(session, category_id)
        return await service.build_product_summaries(session, items, include_availability=True)


# ---------------------------------------------------------------------------
# Products - public/client API
# ---------------------------------------------------------------------------


@router.get("/products", response_model=CatalogPaginatedResponse[ProductSummaryOut])
@limiter.limit("60/minute")
async def products(
    request: Request,
    pagination: PaginationParams = Depends(get_pagination),
    q: str | None = Query(None, min_length=2, max_length=100, description="Recherche full-text (francais)"),
    category_id: int | None = Query(None, description="Filtre par categorie"),
    allergen_slug: str | None = Query(None, description="Slug allergene explicite"),
    dietary_tag_slug: str | None = Query(None, description="Slug dietary tag explicite"),
    allergen: str | None = Query(None, deprecated=True, description="Alias retrocompatible de allergen_slug"),
    dietary_tag: str | None = Query(None, deprecated=True, description="Alias retrocompatible de dietary_tag_slug"),
    price_min: float | None = Query(None, ge=0, description="Prix minimum (inclus)"),
    price_max: float | None = Query(None, ge=0, description="Prix maximum (inclus)"),
    allergen_free: bool = Query(False, description="Ne retourner que les produits sans allergene declare"),
    redis=Depends(get_arq_pool),
):
    slug = _tenant_slug_from_header(request)
    effective_allergen = allergen_slug or allergen
    effective_dietary = dietary_tag_slug or dietary_tag
    is_default_listing = not (
        q
        or category_id is not None
        or effective_allergen
        or effective_dietary
        or price_min is not None
        or price_max is not None
        or allergen_free
    )

    # [PERF] Le listing par defaut (sans filtre) est la vue la plus consultee du
    # catalogue -- mise en cache courte duree (30s) pour absorber le trafic sans
    # servir des donnees perimees longtemps. Les listings filtres/recherches ne
    # sont pas mis en cache (trop de combinaisons possibles, faible reutilisation).
    cache_key = f"catalog:products:{slug}:{pagination.page}:{pagination.page_size}"
    if is_default_listing:
        cached = await get_cached_json(redis, cache_key)
        if cached is not None:
            return cached

    provider = await get_catalog_provider(slug) if is_default_listing else None
    async with get_tenant_session(slug) as session:
        if is_default_listing:
            summaries, total = await provider.get_catalog(session, pagination, redis=redis)
        else:
            offset = (pagination.page - 1) * pagination.page_size
            items, total = await service.search_products(
                session,
                q=q,
                category_id=category_id,
                allergen_slug=effective_allergen,
                dietary_tag_slug=effective_dietary,
                limit=pagination.page_size,
                offset=offset,
                price_min=price_min,
                price_max=price_max,
                allergen_free=allergen_free,
            )
            summaries = await service.build_product_summaries(session, items, include_availability=True)

    response = CatalogPaginatedResponse.build(summaries, total, pagination)
    if is_default_listing:
        await set_cached_json(redis, cache_key, response.model_dump(mode="json"), ttl_seconds=30)
    return response


@router.get("/products/suggestions", response_model=list[ProductSuggestionOut])
@limiter.limit("60/minute")
async def product_suggestions(
    request: Request,
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(8, ge=1, le=20),
):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        return await service.suggest_products(session, q, limit)


@router.get("/products/featured", response_model=list[ProductSummaryOut])
@limiter.limit("60/minute")
async def featured_products(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        items = await service.list_featured_products(session, limit=limit)
        return await service.build_product_summaries(session, items, include_availability=True)


@router.get("/products/{product_id}", response_model=ProductDetailOut)
@limiter.limit("60/minute")
async def product_detail(request: Request, product_id: int):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        return await service.get_product_detail(session, product_id)


# ---------------------------------------------------------------------------
# Products - admin writes
# ---------------------------------------------------------------------------


async def _invalidate_catalog_cache(redis, tenant_slug: str) -> None:
    """Invalide le cache du listing catalogue par defaut pour ce tenant.

    [PERF] Appele apres toute mutation qui change ce qu'un listing catalogue
    par defaut (sans filtre) doit afficher -- create/update/delete produit,
    import CSV. Les mutations de variantes/extras isolees ne l'invalident pas
    explicitement : le TTL de 30s (voir ``products()``) borne deja leur
    fraicheur maximale, un compromis deliberement accepte plutot que
    d'instrumenter chaque route d'ecriture du module.
    """
    await invalidate_prefix(redis, f"catalog:products:{tenant_slug}:")


@router.post("/products", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductCreate,
    current_user=Depends(require_role("admin")),
    redis=Depends(get_arq_pool),
):
    provider = await get_catalog_provider(current_user["tenant_slug"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        product = await provider.create_product(session, body, user_id=_user_id(current_user))
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return product


@router.put("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: int,
    body: ProductUpdate,
    current_user=Depends(require_role("admin")),
    redis=Depends(get_arq_pool),
):
    provider = await get_catalog_provider(current_user["tenant_slug"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        product = await provider.update_product(session, product_id, body, user_id=_user_id(current_user))
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return product


@router.delete("/products/{product_id}", status_code=204)
async def delete_product(
    product_id: int,
    current_user=Depends(require_role("admin")),
    redis=Depends(get_arq_pool),
):
    provider = await get_catalog_provider(current_user["tenant_slug"])
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await provider.delete_product(session, product_id, user_id=_user_id(current_user))
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])


@router.put("/products/{product_id}/override", response_model=ProductOverrideOut)
async def set_product_override(
    product_id: int,
    body: ProductOverrideCreate,
    current_user=Depends(require_role("admin")),
    redis=Depends(get_arq_pool),
):
    """Cree ou remplace l'override de presentation d'un produit synchronise
    depuis un hub POS. Reserve aux produits materialises (``external_product_id``
    renseigne) -- un produit STANDALONE se modifie directement via
    ``PUT /products/{id}``.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404, "product_id")
        if product.external_product_id is None:
            raise AppError(
                "PRODUCT_NOT_SYNCED",
                "Cet override n'a de sens que pour un produit synchronise depuis un hub POS.",
                422,
                "product_id",
            )
        override = await override_repository.upsert_override(session, product_id, body)
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return override


@router.delete("/products/{product_id}/override", status_code=204)
async def delete_product_override(
    product_id: int,
    current_user=Depends(require_role("admin")),
    redis=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        deleted = await override_repository.delete_override(session, product_id)
    if not deleted:
        raise AppError("OVERRIDE_NOT_FOUND", f"No override exists for product {product_id}", 404, "product_id")
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])


@router.post(
    "/products/{product_id}/availability-override",
    response_model=ProductAvailabilityOverrideOut,
    status_code=201,
)
async def set_product_availability_override(
    product_id: int,
    body: ProductAvailabilityOverrideCreate,
    current_user=Depends(require_permission("catalog:availability", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
    redis=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        override = await service.set_product_availability_override(
            session,
            product_id,
            body,
            user_id=_user_id(current_user) or 0,
        )
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return override


@router.get(
    "/products/{product_id}/availability-history",
    response_model=list[ProductAvailabilityOverrideOut],
)
async def product_availability_history(
    product_id: int,
    current_user=Depends(require_permission("catalog:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_product_availability_history(session, product_id)


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


@router.post("/products/{product_id}/variants", response_model=VariantOut, status_code=201)
async def create_variant(
    product_id: int,
    body: VariantCreate,
    current_user=Depends(require_permission("catalog:write", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_variant(session, product_id, body, user_id=_user_id(current_user))


@router.get("/products/{product_id}/variants", response_model=list[VariantOut])
@limiter.limit("60/minute")
async def list_variants(
    request: Request,
    product_id: int,
    current_user=Depends(require_permission("catalog:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_variants(session, product_id)


@router.put("/products/{product_id}/variants/{variant_id}", response_model=VariantOut)
async def update_variant(
    product_id: int,
    variant_id: int,
    body: VariantUpdate,
    current_user=Depends(require_permission("catalog:write", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_variant(
            session,
            product_id,
            variant_id,
            body,
            user_id=_user_id(current_user),
        )


@router.delete("/products/{product_id}/variants/{variant_id}", status_code=204)
async def delete_variant(
    product_id: int,
    variant_id: int,
    current_user=Depends(require_permission("catalog:write", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_variant(session, product_id, variant_id)


# ---------------------------------------------------------------------------
# Extras <-> Products
# ---------------------------------------------------------------------------


@router.post(
    "/products/{product_id}/extras/{extra_id}",
    response_model=ProductExtraLink,
    status_code=201,
)
async def link_extra(
    product_id: int,
    extra_id: int,
    current_user=Depends(require_permission("catalog:write", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.link_extra_to_product(session, product_id, extra_id)
    return ProductExtraLink(product_id=product_id, extra_id=extra_id, linked=True)


@router.delete("/products/{product_id}/extras/{extra_id}", status_code=204)
async def unlink_extra(
    product_id: int,
    extra_id: int,
    current_user=Depends(require_permission("catalog:write", "staff", "admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.unlink_extra_from_product(session, product_id, extra_id)


@router.get("/products/{product_id}/extras", response_model=list[ExtraOut])
@limiter.limit("60/minute")
async def list_product_extras(
    request: Request,
    product_id: int,
    current_user=Depends(require_permission("catalog:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_product_extras(session, product_id)


# ---------------------------------------------------------------------------
# Extras
# ---------------------------------------------------------------------------


@router.get("/extras", response_model=CatalogPaginatedResponse[ExtraOut])
@limiter.limit("60/minute")
async def extras(
    request: Request,
    pagination: PaginationParams = Depends(get_pagination),
):
    slug = _tenant_slug_from_header(request)
    async with get_tenant_session(slug) as session:
        items, total = await service.list_extras(session, pagination)
    return CatalogPaginatedResponse.build(items, total, pagination)


@router.post("/extras", response_model=ExtraOut, status_code=201)
async def create_extra(
    body: ExtraCreate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_extra(session, body, user_id=_user_id(current_user))


@router.put("/extras/{extra_id}", response_model=ExtraOut)
async def update_extra(
    extra_id: int,
    body: ExtraUpdate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_extra(session, extra_id, body, user_id=_user_id(current_user))


@router.delete("/extras/{extra_id}", status_code=204)
async def delete_extra(
    extra_id: int,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    """Supprime definitivement un extra. Refuse (409) s'il est encore lie a un produit."""
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_extra(session, extra_id)


# ---------------------------------------------------------------------------
# Recommendations / bundles simples
# ---------------------------------------------------------------------------


@router.get("/products/{product_id}/recommendations", response_model=list[ProductRecommendationOut])
async def list_recommendations(
    product_id: int,
    current_user=Depends(require_permission("catalog:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_product_recommendations(session, product_id)


@router.post("/products/{product_id}/recommendations", response_model=ProductRecommendationOut, status_code=201)
async def add_recommendation(
    product_id: int,
    body: ProductRecommendationCreate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        recommendation = await service.add_product_recommendation(session, product_id, body)
        items = await service.list_product_recommendations(session, product_id)
        return next(item for item in items if item.id == recommendation.id)


@router.put("/products/{product_id}/recommendations/{recommendation_id}", response_model=ProductRecommendationOut)
async def update_recommendation(
    product_id: int,
    recommendation_id: int,
    body: ProductRecommendationUpdate,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.update_product_recommendation(session, product_id, recommendation_id, body)
        items = await service.list_product_recommendations(session, product_id)
        return next(item for item in items if item.id == recommendation_id)


@router.delete("/products/{product_id}/recommendations/{recommendation_id}", status_code=204)
async def delete_recommendation(
    product_id: int,
    recommendation_id: int,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_product_recommendation(session, product_id, recommendation_id)


# ---------------------------------------------------------------------------
# Import / export CSV
# ---------------------------------------------------------------------------


@router.post("/imports/csv/dry-run", response_model=CatalogCsvDryRunResponse)
async def import_csv_dry_run(
    body: CatalogCsvImportRequest,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.dry_run_catalog_csv(
            session,
            body.csv_text,
            body.filename,
            user_id=_user_id(current_user),
        )


@router.post("/imports/csv/{token}/confirm", response_model=CatalogCsvConfirmResponse)
async def import_csv_confirm(
    token: str,
    current_user=Depends(require_role("admin")),
    _catalog_writable=Depends(require_catalog_writable),
    redis=Depends(get_arq_pool),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await service.confirm_catalog_csv(session, token, user_id=_user_id(current_user))
    await _invalidate_catalog_cache(redis, current_user["tenant_slug"])
    return result


@router.get("/exports/csv", response_class=PlainTextResponse)
async def export_csv(current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        csv_text = await service.export_catalog_csv(session)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=catalog.csv"},
    )


# ---------------------------------------------------------------------------
# Admin completeness and price audit
# ---------------------------------------------------------------------------


@router.get("/admin/completeness")
async def catalog_completeness(current_user=Depends(require_permission("catalog:read", "staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_catalog_completeness(session)


@router.get("/price-audit/{entity_type}/{entity_id}", response_model=CatalogPaginatedResponse[PriceAuditOut])
async def price_audit(
    entity_type: str,
    entity_id: int,
    pagination: PaginationParams = Depends(get_pagination),
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.get_price_audit(session, entity_type, entity_id, pagination)
    return CatalogPaginatedResponse.build(items, total, pagination)


# ---------------------------------------------------------------------------
# Super-admin read-only catalog access
# ---------------------------------------------------------------------------


@router.get(
    "/super-admin/tenants/{tenant_slug}/products",
    response_model=CatalogPaginatedResponse[ProductSummaryOut],
)
async def super_admin_products(
    tenant_slug: str,
    pagination: PaginationParams = Depends(get_pagination),
    current_user=Depends(require_role("super-admin")),
):
    async with get_tenant_session(tenant_slug) as session:
        items, total = await service.list_products(session, pagination, include_inactive=True)
        summaries = await service.build_product_summaries(session, items, include_availability=True)
    return CatalogPaginatedResponse.build(summaries, total, pagination)


@router.get(
    "/super-admin/tenants/{tenant_slug}/products/{product_id}",
    response_model=ProductDetailOut,
)
async def super_admin_product_detail(
    tenant_slug: str,
    product_id: int,
    current_user=Depends(require_role("super-admin")),
):
    async with get_tenant_session(tenant_slug) as session:
        return await service.get_product_detail(session, product_id, include_inactive=True)
