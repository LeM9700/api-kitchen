import csv
import secrets
from io import StringIO

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.catalog.allergen.allergen_models import (
    AllergenDefinition,
    DietaryTag,
    IngredientAllergen,
    ProductAllergen,
    ProductDietaryTag,
)
from app.modules.catalog.image.image_model import MediaImage
from app.modules.catalog.models import (
    CatalogImportBatch,
    CatalogPriceAudit,
    ExtraIngredient,
    Category,
    Extra,
    Product,
    ProductExtra,
    ProductRecommendation,
    ProductVariant,
)
from app.modules.catalog.schemas import (
    CategorySummaryOut,
    CatalogCsvConfirmResponse,
    CatalogCsvDryRunResponse,
    CatalogCsvImportError,
    CatalogCsvImportPreview,
    DietaryTagPublicOut,
    ExtraOut,
    MediaImagePublicOut,
    ProductAllergenPublicOut,
    ProductAvailabilityOut,
    ProductDetailOut,
    ProductRecommendationOut,
    ProductSummaryOut,
    ProductSuggestionOut,
    VariantOut,
)


_PRICE_ENTITY_MODEL = {
    "product": Product,
    "variant": ProductVariant,
    "extra": Extra,
}


def _bool_from_csv(value, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "oui", "active", "actif"}


def _float_from_csv(value, row_num: int, field: str) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Ligne {row_num}: champ {field} invalide") from exc


def _csv_key(row: dict, *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


async def _add_price_audit(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    old_price,
    new_price,
    user_id: int | None,
    source: str,
    reason: str | None,
) -> None:
    if old_price is not None and float(old_price) == float(new_price):
        return
    session.add(
        CatalogPriceAudit(
            entity_type=entity_type,
            entity_id=entity_id,
            old_price=old_price,
            new_price=new_price,
            changed_by_user_id=user_id,
            source=source,
            reason=reason,
        )
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


async def list_categories(
    session: AsyncSession,
    pagination: PaginationParams,
) -> tuple[list[Category], int]:
    base_filter = Category.is_active.is_(True)
    total = await session.scalar(select(func.count()).select_from(Category).where(base_filter)) or 0
    result = await session.execute(
        select(Category)
        .where(base_filter)
        .order_by(Category.display_order, Category.name)
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


async def list_products(
    session: AsyncSession,
    pagination: PaginationParams,
    include_inactive: bool = False,
) -> tuple[list[Product], int]:
    stmt = select(Product)
    count_stmt = select(func.count()).select_from(Product)
    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
        count_stmt = count_stmt.where(Product.is_active.is_(True))
    total = await session.scalar(count_stmt) or 0
    result = await session.execute(
        stmt.order_by(Product.name)
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


async def create_product(
    session: AsyncSession,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> Product:
    product = Product(**body.model_dump())
    session.add(product)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        if "products_category_id_fkey" in str(e.orig):
            raise HTTPException(status_code=422, detail="category_id invalide : catégorie introuvable.")
        raise
    await _add_price_audit(
        session,
        "product",
        product.id,
        None,
        product.base_price,
        user_id,
        source,
        "creation",
    )
    await session.commit()
    await session.refresh(product)
    return product


async def update_product(
    session: AsyncSession,
    product_id: int,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)

    data = body.model_dump(exclude_unset=True)
    reason = data.pop("price_change_reason", None)
    if "base_price" in data:
        await _add_price_audit(
            session,
            "product",
            product.id,
            product.base_price,
            data["base_price"],
            user_id,
            source,
            reason,
        )
    for key, value in data.items():
        setattr(product, key, value)
    await session.commit()
    await session.refresh(product)
    return product


async def _category_map(session: AsyncSession, products: list[Product]) -> dict[int, Category]:
    category_ids = {p.category_id for p in products if p.category_id is not None}
    if not category_ids:
        return {}
    result = await session.execute(select(Category).where(Category.id.in_(category_ids)))
    return {category.id: category for category in result.scalars()}


async def _gallery_map(session: AsyncSession, product_ids: list[int]) -> dict[int, list[MediaImage]]:
    if not product_ids:
        return {}
    result = await session.execute(
        select(MediaImage)
        .where(MediaImage.entity_type == "product", MediaImage.entity_id.in_(product_ids))
        .order_by(MediaImage.entity_id, MediaImage.display_order)
    )
    images_by_product: dict[int, list[MediaImage]] = {}
    for image in result.scalars():
        images_by_product.setdefault(image.entity_id, []).append(image)
    return images_by_product


async def _allergen_map(
    session: AsyncSession,
    product_ids: list[int],
) -> tuple[dict[int, list[ProductAllergenPublicOut]], dict[int, bool]]:
    if not product_ids:
        return {}, {}

    regulatory_result = await session.execute(
        select(AllergenDefinition.id).where(AllergenDefinition.is_regulatory.is_(True))
    )
    regulatory_ids = set(regulatory_result.scalars())

    result = await session.execute(
        select(ProductAllergen, AllergenDefinition)
        .join(AllergenDefinition, AllergenDefinition.id == ProductAllergen.allergen_id)
        .where(ProductAllergen.product_id.in_(product_ids))
        .order_by(AllergenDefinition.is_regulatory.desc(), AllergenDefinition.name)
    )
    allergens_by_product: dict[int, list[ProductAllergenPublicOut]] = {}
    declared_by_product: dict[int, set[int]] = {pid: set() for pid in product_ids}
    for pa, definition in result.all():
        declared_by_product.setdefault(pa.product_id, set()).add(pa.allergen_id)
        allergens_by_product.setdefault(pa.product_id, []).append(
            ProductAllergenPublicOut(
                allergen_id=pa.allergen_id,
                name=definition.name,
                slug=definition.slug,
                level=pa.level,
                source=pa.source,
                is_regulatory=definition.is_regulatory,
            )
        )

    completeness = {
        product_id: regulatory_ids.issubset(declared_by_product.get(product_id, set()))
        for product_id in product_ids
    }
    return allergens_by_product, completeness


async def _dietary_map(session: AsyncSession, product_ids: list[int]) -> dict[int, list[DietaryTagPublicOut]]:
    if not product_ids:
        return {}
    result = await session.execute(
        select(ProductDietaryTag.product_id, DietaryTag)
        .join(DietaryTag, DietaryTag.id == ProductDietaryTag.dietary_tag_id)
        .where(ProductDietaryTag.product_id.in_(product_ids))
        .order_by(DietaryTag.name)
    )
    tags_by_product: dict[int, list[DietaryTagPublicOut]] = {}
    for product_id, tag in result.all():
        tags_by_product.setdefault(product_id, []).append(
            DietaryTagPublicOut(id=tag.id, name=tag.name, slug=tag.slug)
        )
    return tags_by_product


async def _availability_map(session: AsyncSession, product_ids: list[int]) -> dict[int, ProductAvailabilityOut]:
    if not product_ids:
        return {}
    from app.modules.stock import service as stock_service

    try:
        raw_availability = await stock_service.get_products_availability(session, product_ids)
    except Exception:
        raw_availability = {}

    availability: dict[int, ProductAvailabilityOut] = {}
    for product_id in product_ids:
        data = raw_availability.get(
            product_id,
            {"product_id": product_id, "available": True, "limiting_ingredient": None},
        )
        availability[product_id] = ProductAvailabilityOut(**data)
    return availability


async def build_product_summaries(
    session: AsyncSession,
    products: list[Product],
    include_availability: bool = True,
) -> list[ProductSummaryOut]:
    product_ids = [product.id for product in products]
    categories = await _category_map(session, products)
    gallery = await _gallery_map(session, product_ids)
    allergens, completeness = await _allergen_map(session, product_ids)
    dietary_tags = await _dietary_map(session, product_ids)
    availability = await _availability_map(session, product_ids) if include_availability else {}

    summaries: list[ProductSummaryOut] = []
    for product in products:
        images = gallery.get(product.id, [])
        primary = next((image for image in images if image.is_primary), images[0] if images else None)
        category = categories.get(product.category_id) if product.category_id is not None else None
        summaries.append(
            ProductSummaryOut(
                id=product.id,
                category_id=product.category_id,
                name=product.name,
                description=product.description,
                base_price=float(product.base_price),
                image_url=product.image_url,
                is_active=product.is_active,
                category=CategorySummaryOut.model_validate(category) if category else None,
                primary_image=MediaImagePublicOut.model_validate(primary) if primary else None,
                allergens=allergens.get(product.id, []),
                dietary_tags=dietary_tags.get(product.id, []),
                availability=availability.get(product.id),
                regulatory_complete=completeness.get(product.id, False),
            )
        )
    return summaries


async def get_product_detail(
    session: AsyncSession,
    product_id: int,
    include_inactive: bool = False,
    include_availability: bool = True,
) -> ProductDetailOut:
    product = await session.get(Product, product_id)
    if product is None or (not include_inactive and not product.is_active):
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)

    summary = (await build_product_summaries(session, [product], include_availability))[0]
    variants = await list_variants(session, product_id, include_inactive=include_inactive)
    extras = await list_product_extras(session, product_id, include_inactive=include_inactive)
    gallery = await _gallery_map(session, [product_id])
    recommended_products = await list_recommended_products(session, product_id)

    return ProductDetailOut(
        **summary.model_dump(),
        variants=[VariantOut.model_validate(variant) for variant in variants],
        extras=[ExtraOut.model_validate(extra) for extra in extras],
        gallery=[MediaImagePublicOut.model_validate(image) for image in gallery.get(product_id, [])],
        recommendations=recommended_products,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search_products(
    session: AsyncSession,
    q: str | None,
    category_id: int | None,
    allergen_slug: str | None,
    dietary_tag_slug: str | None,
    limit: int,
    offset: int,
    include_inactive: bool = False,
    price_min: float | None = None,
    price_max: float | None = None,
    allergen_free: bool = False,
) -> tuple[list[Product], int]:
    stmt = select(Product)
    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))

    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)

    if price_min is not None:
        stmt = stmt.where(Product.base_price >= price_min)

    if price_max is not None:
        stmt = stmt.where(Product.base_price <= price_max)

    if allergen_free:
        # Aucun allergene "present" declare sur le produit.
        stmt = stmt.where(
            text(
                "NOT EXISTS ("
                "  SELECT 1 FROM product_allergens pa"
                "  WHERE pa.product_id = products.id AND pa.level = 'present'"
                ")"
            )
        )

    if q:
        stmt = stmt.where(
            text(
                "to_tsvector('french', products.name || ' ' || COALESCE(products.description, '')) "
                "@@ plainto_tsquery('french', :q)"
            ).bindparams(q=q)
        )

    if allergen_slug:
        stmt = stmt.where(
            text(
                "EXISTS ("
                "  SELECT 1 FROM product_allergens pa"
                "  JOIN allergen_definitions ad ON pa.allergen_id = ad.id"
                "  WHERE pa.product_id = products.id"
                "  AND ad.slug = :allergen_slug"
                "  AND pa.level = 'present'"
                ")"
            ).bindparams(allergen_slug=allergen_slug)
        )

    if dietary_tag_slug:
        stmt = stmt.where(
            text(
                "EXISTS ("
                "  SELECT 1 FROM product_dietary_tags pdt"
                "  JOIN dietary_tags dt ON pdt.dietary_tag_id = dt.id"
                "  WHERE pdt.product_id = products.id"
                "  AND dt.slug = :dietary_tag_slug"
                ")"
            ).bindparams(dietary_tag_slug=dietary_tag_slug)
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    if q:
        stmt = stmt.order_by(
            text(
                "ts_rank("
                "  to_tsvector('french', products.name || ' ' || COALESCE(products.description, '')),"
                "  plainto_tsquery('french', :q)"
                ") DESC"
            ).bindparams(q=q)
        )

    stmt = stmt.order_by(
        text("(SELECT COUNT(*) FROM order_items oi WHERE oi.product_id = products.id) DESC NULLS LAST"),
        Product.name,
    ).limit(limit).offset(offset)

    result = await session.execute(stmt)
    return list(result.scalars()), total


async def suggest_products(session: AsyncSession, q: str, limit: int = 8) -> list[ProductSuggestionOut]:
    pattern = f"%{q.strip()}%"
    result = await session.execute(
        select(Product)
        .where(Product.is_active.is_(True), Product.name.ilike(pattern))
        .order_by(Product.name)
        .limit(limit)
    )
    products = list(result.scalars())
    gallery = await _gallery_map(session, [product.id for product in products])
    suggestions: list[ProductSuggestionOut] = []
    for product in products:
        images = gallery.get(product.id, [])
        primary = next((image for image in images if image.is_primary), images[0] if images else None)
        suggestions.append(
            ProductSuggestionOut(
                id=product.id,
                name=product.name,
                category_id=product.category_id,
                primary_image_url=primary.url_thumbnail if primary else product.image_url,
            )
        )
    return suggestions


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


async def create_variant(
    session: AsyncSession,
    product_id: int,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> ProductVariant:
    product = await session.get(Product, product_id)
    if product is None or not product.is_active:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found or inactive", 404)

    variant = ProductVariant(
        product_id=product_id,
        name=body.name,
        price_delta=body.price_delta,
        is_active=body.is_available,
    )
    session.add(variant)
    await session.flush()
    await _add_price_audit(session, "variant", variant.id, None, variant.price_delta, user_id, source, "creation")
    await session.commit()
    await session.refresh(variant)
    return variant


async def list_variants(
    session: AsyncSession,
    product_id: int,
    include_inactive: bool = False,
) -> list[ProductVariant]:
    product = await session.get(Product, product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)

    stmt = select(ProductVariant).where(ProductVariant.product_id == product_id)
    if not include_inactive:
        stmt = stmt.where(ProductVariant.is_active.is_(True))
    result = await session.execute(stmt.order_by(ProductVariant.name))
    return list(result.scalars())


async def update_variant(
    session: AsyncSession,
    product_id: int,
    variant_id: int,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> ProductVariant:
    variant = await session.get(ProductVariant, variant_id)
    if variant is None or variant.product_id != product_id:
        raise AppError("VARIANT_NOT_FOUND", f"Variant {variant_id} not found for product {product_id}", 404)

    data = body.model_dump(exclude_unset=True)
    reason = data.pop("price_change_reason", None)
    if "name" in data:
        variant.name = data["name"]
    if "price_delta" in data:
        await _add_price_audit(
            session,
            "variant",
            variant.id,
            variant.price_delta,
            data["price_delta"],
            user_id,
            source,
            reason,
        )
        variant.price_delta = data["price_delta"]
    if "is_available" in data:
        variant.is_active = data["is_available"]

    await session.commit()
    await session.refresh(variant)
    return variant


async def delete_variant(session: AsyncSession, product_id: int, variant_id: int) -> None:
    variant = await session.get(ProductVariant, variant_id)
    if variant is None or variant.product_id != product_id:
        raise AppError("VARIANT_NOT_FOUND", f"Variant {variant_id} not found for product {product_id}", 404)
    variant.is_active = False
    await session.commit()


# ---------------------------------------------------------------------------
# Extras
# ---------------------------------------------------------------------------


async def list_extras(
    session: AsyncSession,
    pagination: PaginationParams,
    include_inactive: bool = False,
) -> tuple[list[Extra], int]:
    stmt = select(Extra)
    count_stmt = select(func.count()).select_from(Extra)
    if not include_inactive:
        stmt = stmt.where(Extra.is_active.is_(True))
        count_stmt = count_stmt.where(Extra.is_active.is_(True))
    total = await session.scalar(count_stmt) or 0
    result = await session.execute(
        stmt.order_by(Extra.name)
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


async def create_extra(
    session: AsyncSession,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> Extra:
    extra = Extra(**body.model_dump())
    session.add(extra)
    await session.flush()
    await _add_price_audit(session, "extra", extra.id, None, extra.price, user_id, source, "creation")
    await session.commit()
    await session.refresh(extra)
    return extra


async def update_extra(
    session: AsyncSession,
    extra_id: int,
    body,
    user_id: int | None = None,
    source: str = "admin",
) -> Extra:
    extra = await session.get(Extra, extra_id)
    if extra is None:
        raise AppError("EXTRA_NOT_FOUND", f"Extra {extra_id} not found", 404)
    data = body.model_dump(exclude_unset=True)
    reason = data.pop("price_change_reason", None)
    if "price" in data:
        await _add_price_audit(session, "extra", extra.id, extra.price, data["price"], user_id, source, reason)
    for key, value in data.items():
        setattr(extra, key, value)
    await session.commit()
    await session.refresh(extra)
    return extra


async def delete_extra(session: AsyncSession, extra_id: int) -> None:
    """Supprime definitivement un extra.

    Refuse la suppression tant que l'extra est encore lie a au moins un
    produit (product_extras) — l'admin doit d'abord le delier via
    ``DELETE /products/{product_id}/extras/{extra_id}``.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        extra_id: Cle primaire de l'extra a supprimer.

    Raises:
        AppError: EXTRA_NOT_FOUND (404) si l'extra n'existe pas.
        AppError: EXTRA_STILL_LINKED (409) si l'extra est encore lie a un produit.
    """
    extra = await session.get(Extra, extra_id)
    if extra is None:
        raise AppError("EXTRA_NOT_FOUND", f"Extra {extra_id} not found", 404)

    linked_count = await session.scalar(
        select(func.count()).select_from(ProductExtra).where(ProductExtra.extra_id == extra_id)
    )
    if linked_count:
        raise AppError(
            "EXTRA_STILL_LINKED",
            f"Extra {extra_id} is still linked to {linked_count} product(s); unlink it first",
            409,
        )

    await session.delete(extra)
    await session.commit()


async def link_extra_to_product(session: AsyncSession, product_id: int, extra_id: int) -> None:
    product = await session.get(Product, product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)
    extra = await session.get(Extra, extra_id)
    if extra is None:
        raise AppError("EXTRA_NOT_FOUND", f"Extra {extra_id} not found", 404)

    existing = await session.get(ProductExtra, (product_id, extra_id))
    if existing is None:
        session.add(ProductExtra(product_id=product_id, extra_id=extra_id))
        await session.commit()


async def unlink_extra_from_product(session: AsyncSession, product_id: int, extra_id: int) -> None:
    existing = await session.get(ProductExtra, (product_id, extra_id))
    if existing is None:
        raise AppError("EXTRA_NOT_FOUND", f"Extra {extra_id} is not linked to product {product_id}", 404)
    await session.delete(existing)
    await session.commit()


async def list_product_extras(
    session: AsyncSession,
    product_id: int,
    include_inactive: bool = False,
) -> list[Extra]:
    product = await session.get(Product, product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)

    stmt = (
        select(Extra)
        .join(ProductExtra, ProductExtra.extra_id == Extra.id)
        .where(ProductExtra.product_id == product_id)
    )
    if not include_inactive:
        stmt = stmt.where(Extra.is_active.is_(True))
    result = await session.execute(stmt.order_by(Extra.name))
    return list(result.scalars())


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


async def add_product_recommendation(session: AsyncSession, product_id: int, body) -> ProductRecommendation:
    product = await session.get(Product, product_id)
    recommended = await session.get(Product, body.recommended_product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found", 404)
    if recommended is None:
        raise AppError("RECOMMENDED_PRODUCT_NOT_FOUND", f"Product {body.recommended_product_id} not found", 404)
    if product_id == body.recommended_product_id:
        raise AppError("INVALID_RECOMMENDATION", "A product cannot recommend itself", 422)

    existing = await session.scalar(
        select(ProductRecommendation).where(
            ProductRecommendation.product_id == product_id,
            ProductRecommendation.recommended_product_id == body.recommended_product_id,
        )
    )
    if existing:
        existing.display_order = body.display_order
        existing.label = body.label
        existing.is_active = True
        await session.commit()
        await session.refresh(existing)
        return existing

    recommendation = ProductRecommendation(
        product_id=product_id,
        recommended_product_id=body.recommended_product_id,
        display_order=body.display_order,
        label=body.label,
    )
    session.add(recommendation)
    await session.commit()
    await session.refresh(recommendation)
    return recommendation


async def update_product_recommendation(
    session: AsyncSession,
    product_id: int,
    recommendation_id: int,
    body,
) -> ProductRecommendation:
    recommendation = await session.get(ProductRecommendation, recommendation_id)
    if recommendation is None or recommendation.product_id != product_id:
        raise AppError("RECOMMENDATION_NOT_FOUND", "Recommendation not found", 404)
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(recommendation, key, value)
    await session.commit()
    await session.refresh(recommendation)
    return recommendation


async def delete_product_recommendation(session: AsyncSession, product_id: int, recommendation_id: int) -> None:
    recommendation = await session.get(ProductRecommendation, recommendation_id)
    if recommendation is None or recommendation.product_id != product_id:
        raise AppError("RECOMMENDATION_NOT_FOUND", "Recommendation not found", 404)
    recommendation.is_active = False
    await session.commit()


async def list_recommended_products(session: AsyncSession, product_id: int) -> list[ProductSummaryOut]:
    result = await session.execute(
        select(Product)
        .join(ProductRecommendation, ProductRecommendation.recommended_product_id == Product.id)
        .where(
            ProductRecommendation.product_id == product_id,
            ProductRecommendation.is_active.is_(True),
            Product.is_active.is_(True),
        )
        .order_by(ProductRecommendation.display_order, Product.name)
    )
    return await build_product_summaries(session, list(result.scalars()), include_availability=True)


async def list_product_recommendations(session: AsyncSession, product_id: int) -> list[ProductRecommendationOut]:
    result = await session.execute(
        select(ProductRecommendation)
        .where(ProductRecommendation.product_id == product_id, ProductRecommendation.is_active.is_(True))
        .order_by(ProductRecommendation.display_order)
    )
    recommendations = list(result.scalars())
    products_by_id: dict[int, ProductSummaryOut] = {}
    if recommendations:
        product_result = await session.execute(
            select(Product).where(Product.id.in_([r.recommended_product_id for r in recommendations]))
        )
        summaries = await build_product_summaries(session, list(product_result.scalars()), include_availability=True)
        products_by_id = {summary.id: summary for summary in summaries}
    return [
        ProductRecommendationOut(
            id=item.id,
            product_id=item.product_id,
            recommended_product_id=item.recommended_product_id,
            display_order=item.display_order,
            label=item.label,
            is_active=item.is_active,
            product=products_by_id.get(item.recommended_product_id),
        )
        for item in recommendations
    ]


# ---------------------------------------------------------------------------
# Allergens and order validation helpers
# ---------------------------------------------------------------------------


async def get_selection_allergens(
    session: AsyncSession,
    product_id: int,
    variant_id: int | None = None,
    extra_ids: list[int] | None = None,
) -> tuple[list[ProductAllergenPublicOut], bool]:
    from app.modules.stock.models import ProductIngredient, VariantIngredient

    extra_ids = extra_ids or []
    ingredient_ids: set[int] = set()

    product_ingredients = await session.execute(
        select(ProductIngredient.ingredient_id).where(ProductIngredient.product_id == product_id)
    )
    ingredient_ids.update(product_ingredients.scalars())

    if variant_id is not None:
        variant = await session.get(ProductVariant, variant_id)
        if variant is None or variant.product_id != product_id or not variant.is_active:
            raise AppError("VARIANT_NOT_FOUND", f"Variant {variant_id} not found for product {product_id}", 404)
        variant_ingredients = await session.execute(
            select(VariantIngredient.ingredient_id).where(VariantIngredient.variant_id == variant_id)
        )
        ingredient_ids.update(variant_ingredients.scalars())

    if extra_ids:
        linked = await session.execute(
            select(ProductExtra.extra_id).where(
                ProductExtra.product_id == product_id,
                ProductExtra.extra_id.in_(extra_ids),
            )
        )
        linked_ids = set(linked.scalars())
        if linked_ids != set(extra_ids):
            raise AppError("EXTRA_NOT_LINKED", "One or more extras are not linked to this product", 422)
        from app.modules.catalog.models import ExtraIngredient

        extra_ingredients = await session.execute(
            select(ExtraIngredient.ingredient_id).where(ExtraIngredient.extra_id.in_(extra_ids))
        )
        ingredient_ids.update(extra_ingredients.scalars())

    product_summary = await _allergen_map(session, [product_id])
    declared = {item.allergen_id: item for item in product_summary[0].get(product_id, [])}

    if ingredient_ids:
        result = await session.execute(
            select(IngredientAllergen, AllergenDefinition)
            .join(AllergenDefinition, AllergenDefinition.id == IngredientAllergen.allergen_id)
            .where(IngredientAllergen.ingredient_id.in_(ingredient_ids))
        )
        for ingredient_allergen, definition in result.all():
            existing = declared.get(ingredient_allergen.allergen_id)
            if existing is None or _allergen_priority(ingredient_allergen.level) > _allergen_priority(existing.level):
                declared[ingredient_allergen.allergen_id] = ProductAllergenPublicOut(
                    allergen_id=definition.id,
                    name=definition.name,
                    slug=definition.slug,
                    level=ingredient_allergen.level,
                    source="ingredient",
                    is_regulatory=definition.is_regulatory,
                )

    regulatory_result = await session.execute(
        select(AllergenDefinition.id).where(AllergenDefinition.is_regulatory.is_(True))
    )
    regulatory_ids = set(regulatory_result.scalars())
    regulatory_complete = regulatory_ids.issubset(set(declared))
    return sorted(declared.values(), key=lambda item: (not item.is_regulatory, item.name)), regulatory_complete


def _allergen_priority(level: str) -> int:
    return {"present": 2, "traces": 1, "absent": 0}.get(level, 0)


async def validate_catalog_selection_for_order(
    session: AsyncSession,
    product_id: int,
    variant_id: int | None = None,
    extra_ids: list[int] | None = None,
) -> tuple[list[ProductAllergenPublicOut], bool]:
    product = await session.get(Product, product_id)
    if product is None or not product.is_active:
        raise AppError("PRODUCT_NOT_FOUND", f"Product {product_id} not found or inactive", 404)
    allergens, regulatory_complete = await get_selection_allergens(session, product_id, variant_id, extra_ids)
    if not regulatory_complete:
        raise AppError(
            "ALLERGEN_DECLARATION_INCOMPLETE",
            "Les allergenes reglementaires ne sont pas tous declares pour cette selection.",
            422,
        )
    return allergens, regulatory_complete


async def get_catalog_completeness(session: AsyncSession) -> dict:
    products_result = await session.execute(select(Product).where(Product.is_active.is_(True)))
    products = list(products_result.scalars())
    _, completeness = await _allergen_map(session, [product.id for product in products])
    complete = sum(1 for product in products if completeness.get(product.id, False))
    total = len(products)
    return {
        "total_products": total,
        "complete_products": complete,
        "completion_percent": 100.0 if total == 0 else round((complete / total) * 100, 2),
    }


# ---------------------------------------------------------------------------
# Price audit
# ---------------------------------------------------------------------------


async def get_price_audit(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    pagination: PaginationParams,
) -> tuple[list[CatalogPriceAudit], int]:
    if entity_type not in _PRICE_ENTITY_MODEL:
        raise AppError("INVALID_ENTITY_TYPE", "entity_type must be product, variant or extra", 422)
    base = CatalogPriceAudit.entity_type == entity_type
    if entity_id:
        base = base & (CatalogPriceAudit.entity_id == entity_id)
    total = await session.scalar(select(func.count()).select_from(CatalogPriceAudit).where(base)) or 0
    result = await session.execute(
        select(CatalogPriceAudit)
        .where(base)
        .order_by(CatalogPriceAudit.changed_at.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


# ---------------------------------------------------------------------------
# CSV import/export
# ---------------------------------------------------------------------------


_CSV_TYPES = {
    "category",
    "product",
    "variant",
    "extra",
    "product_extra",
    "product_allergen",
    "dietary_tag",
    "product_dietary_tag",
    "recommendation",
    "extra_ingredient",
}


def _read_csv_rows(csv_text: str) -> list[tuple[int, dict]]:
    reader = csv.DictReader(StringIO(csv_text))
    rows: list[tuple[int, dict]] = []
    for row_num, row in enumerate(reader, start=2):
        normalized = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        rows.append((row_num, normalized))
    return rows


def _validate_csv_rows(rows: list[tuple[int, dict]]) -> tuple[list[CatalogCsvImportPreview], list[CatalogCsvImportError]]:
    previews: list[CatalogCsvImportPreview] = []
    errors: list[CatalogCsvImportError] = []
    for row_num, row in rows:
        row_type = (_csv_key(row, "type", "entity_type") or "").lower()
        if row_type not in _CSV_TYPES:
            errors.append(CatalogCsvImportError(row=row_num, code="INVALID_TYPE", detail=f"Type CSV inconnu: {row_type}"))
            continue
        key = _csv_key(row, "name", "product_name", "slug", "recommended_product_name", "extra_name") or f"row-{row_num}"
        required_missing = False
        if row_type in {"category", "product", "extra", "dietary_tag"} and not _csv_key(row, "name", "product_name", "extra_name"):
            required_missing = True
        if row_type == "product" and not _csv_key(row, "base_price", "price"):
            required_missing = True
        if row_type == "variant" and (not _csv_key(row, "product_name", "product_id") or not _csv_key(row, "name", "variant_name")):
            required_missing = True
        if row_type in {"product_extra", "product_allergen", "product_dietary_tag", "recommendation"} and not _csv_key(row, "product_name", "product_id"):
            required_missing = True
        if row_type == "extra_ingredient" and (not _csv_key(row, "extra_name", "extra_id") or not _csv_key(row, "ingredient_id") or not _csv_key(row, "quantity")):
            required_missing = True
        if required_missing:
            errors.append(CatalogCsvImportError(row=row_num, code="MISSING_FIELD", detail="Champ obligatoire manquant"))
            continue
        previews.append(CatalogCsvImportPreview(row=row_num, action="upsert", entity_type=row_type, key=key))
    return previews, errors


async def dry_run_catalog_csv(
    session: AsyncSession,
    csv_text: str,
    filename: str | None,
    user_id: int | None,
) -> CatalogCsvDryRunResponse:
    rows = _read_csv_rows(csv_text)
    previews, errors = _validate_csv_rows(rows)
    token = secrets.token_urlsafe(24)
    response = CatalogCsvDryRunResponse(
        token=token,
        valid=not errors,
        total_rows=len(rows),
        previews=previews,
        errors=errors,
    )
    session.add(
        CatalogImportBatch(
            token=token,
            filename=filename,
            csv_text=csv_text,
            status="dry_run",
            validation_report=response.model_dump(),
            created_by_user_id=user_id,
        )
    )
    await session.commit()
    return response


async def _find_product(session: AsyncSession, row: dict) -> Product | None:
    product_id = _csv_key(row, "product_id")
    if product_id:
        return await session.get(Product, int(product_id))
    product_name = _csv_key(row, "product_name", "name")
    if not product_name:
        return None
    return await session.scalar(select(Product).where(func.lower(Product.name) == product_name.lower()))


async def _find_extra(session: AsyncSession, row: dict) -> Extra | None:
    extra_id = _csv_key(row, "extra_id")
    if extra_id:
        return await session.get(Extra, int(extra_id))
    extra_name = _csv_key(row, "extra_name", "name")
    if not extra_name:
        return None
    return await session.scalar(select(Extra).where(func.lower(Extra.name) == extra_name.lower()))


async def confirm_catalog_csv(
    session: AsyncSession,
    token: str,
    user_id: int | None,
) -> CatalogCsvConfirmResponse:
    batch = await session.scalar(select(CatalogImportBatch).where(CatalogImportBatch.token == token))
    if batch is None:
        raise AppError("IMPORT_NOT_FOUND", "CSV import dry-run not found", 404)
    if batch.status != "dry_run":
        raise AppError("IMPORT_ALREADY_PROCESSED", "CSV import has already been processed", 409)
    if not batch.validation_report.get("valid", False):
        raise AppError("IMPORT_INVALID", "CSV import dry-run contains errors", 422)

    rows = _read_csv_rows(batch.csv_text)
    created = updated = linked = 0
    errors: list[CatalogCsvImportError] = []

    for row_num, row in rows:
        row_type = (_csv_key(row, "type", "entity_type") or "").lower()
        try:
            if row_type == "category":
                name = _csv_key(row, "name") or ""
                category = await session.scalar(select(Category).where(func.lower(Category.name) == name.lower()))
                if category is None:
                    category = Category(name=name)
                    session.add(category)
                    created += 1
                else:
                    updated += 1
                category.display_order = int(_csv_key(row, "display_order") or 0)
                category.is_active = _bool_from_csv(_csv_key(row, "is_active"), True)

            elif row_type == "product":
                name = _csv_key(row, "name", "product_name") or ""
                product = await session.scalar(select(Product).where(func.lower(Product.name) == name.lower()))
                old_price = product.base_price if product else None
                if product is None:
                    product = Product(name=name, base_price=0)
                    session.add(product)
                    await session.flush()
                    created += 1
                else:
                    updated += 1
                category_name = _csv_key(row, "category_name")
                if category_name:
                    category = await session.scalar(select(Category).where(func.lower(Category.name) == category_name.lower()))
                    product.category_id = category.id if category else None
                product.description = _csv_key(row, "description")
                product.base_price = _float_from_csv(_csv_key(row, "base_price", "price"), row_num, "base_price")
                product.image_url = _csv_key(row, "image_url")
                product.is_active = _bool_from_csv(_csv_key(row, "is_active"), True)
                await _add_price_audit(session, "product", product.id, old_price, product.base_price, user_id, "import", "csv import")

            elif row_type == "variant":
                product = await _find_product(session, row)
                if product is None:
                    raise ValueError(f"Ligne {row_num}: produit introuvable")
                name = _csv_key(row, "variant_name", "name") or ""
                variant = await session.scalar(
                    select(ProductVariant).where(ProductVariant.product_id == product.id, func.lower(ProductVariant.name) == name.lower())
                )
                old_price = variant.price_delta if variant else None
                if variant is None:
                    variant = ProductVariant(product_id=product.id, name=name, price_delta=0)
                    session.add(variant)
                    await session.flush()
                    created += 1
                else:
                    updated += 1
                variant.price_delta = _float_from_csv(_csv_key(row, "price_delta", "base_price", "price"), row_num, "price_delta")
                variant.is_active = _bool_from_csv(_csv_key(row, "is_active"), True)
                await _add_price_audit(session, "variant", variant.id, old_price, variant.price_delta, user_id, "import", "csv import")

            elif row_type == "extra":
                name = _csv_key(row, "extra_name", "name") or ""
                extra = await session.scalar(select(Extra).where(func.lower(Extra.name) == name.lower()))
                old_price = extra.price if extra else None
                if extra is None:
                    extra = Extra(name=name, price=0)
                    session.add(extra)
                    await session.flush()
                    created += 1
                else:
                    updated += 1
                extra.price = _float_from_csv(_csv_key(row, "price", "base_price"), row_num, "price")
                extra.is_active = _bool_from_csv(_csv_key(row, "is_active"), True)
                await _add_price_audit(session, "extra", extra.id, old_price, extra.price, user_id, "import", "csv import")

            elif row_type == "product_extra":
                product = await _find_product(session, row)
                extra = await _find_extra(session, row)
                if product is None or extra is None:
                    raise ValueError(f"Ligne {row_num}: produit ou extra introuvable")
                if await session.get(ProductExtra, (product.id, extra.id)) is None:
                    session.add(ProductExtra(product_id=product.id, extra_id=extra.id))
                    linked += 1

            elif row_type == "product_allergen":
                product = await _find_product(session, row)
                slug = _csv_key(row, "allergen_slug", "slug")
                allergen = await session.scalar(select(AllergenDefinition).where(AllergenDefinition.slug == slug))
                if product is None or allergen is None:
                    raise ValueError(f"Ligne {row_num}: produit ou allergene introuvable")
                level = _csv_key(row, "level") or "present"
                existing = await session.get(ProductAllergen, (product.id, allergen.id))
                if existing:
                    existing.level = level
                    existing.source = "manual"
                    updated += 1
                else:
                    session.add(ProductAllergen(product_id=product.id, allergen_id=allergen.id, level=level, source="manual"))
                    created += 1

            elif row_type == "dietary_tag":
                name = _csv_key(row, "name") or ""
                slug = _csv_key(row, "slug") or name.lower().replace(" ", "-")
                tag = await session.scalar(select(DietaryTag).where(DietaryTag.slug == slug))
                if tag is None:
                    session.add(DietaryTag(name=name, slug=slug))
                    created += 1
                else:
                    tag.name = name
                    updated += 1

            elif row_type == "product_dietary_tag":
                product = await _find_product(session, row)
                slug = _csv_key(row, "dietary_slug", "slug")
                tag = await session.scalar(select(DietaryTag).where(DietaryTag.slug == slug))
                if product is None or tag is None:
                    raise ValueError(f"Ligne {row_num}: produit ou tag introuvable")
                if await session.get(ProductDietaryTag, (product.id, tag.id)) is None:
                    session.add(ProductDietaryTag(product_id=product.id, dietary_tag_id=tag.id))
                    linked += 1

            elif row_type == "recommendation":
                product = await _find_product(session, row)
                recommended_name = _csv_key(row, "recommended_product_name", "recommended_name")
                recommended_id = _csv_key(row, "recommended_product_id")
                recommended = await session.get(Product, int(recommended_id)) if recommended_id else None
                if recommended is None and recommended_name:
                    recommended = await session.scalar(select(Product).where(func.lower(Product.name) == recommended_name.lower()))
                if product is None or recommended is None:
                    raise ValueError(f"Ligne {row_num}: recommandation invalide")
                if product.id == recommended.id:
                    raise ValueError(f"Ligne {row_num}: un produit ne peut pas se recommander lui-meme")
                existing = await session.scalar(
                    select(ProductRecommendation).where(
                        ProductRecommendation.product_id == product.id,
                        ProductRecommendation.recommended_product_id == recommended.id,
                    )
                )
                if existing is None:
                    session.add(
                        ProductRecommendation(
                            product_id=product.id,
                            recommended_product_id=recommended.id,
                            display_order=int(_csv_key(row, "display_order") or 0),
                            label=_csv_key(row, "label"),
                        )
                    )
                    linked += 1
                else:
                    existing.display_order = int(_csv_key(row, "display_order") or existing.display_order)
                    existing.label = _csv_key(row, "label")
                    existing.is_active = True
                    updated += 1

            elif row_type == "extra_ingredient":
                extra = await _find_extra(session, row)
                ingredient_id = _csv_key(row, "ingredient_id")
                if extra is None or ingredient_id is None:
                    raise ValueError(f"Ligne {row_num}: extra ou ingredient introuvable")
                existing = await session.scalar(
                    select(ExtraIngredient).where(
                        ExtraIngredient.extra_id == extra.id,
                        ExtraIngredient.ingredient_id == int(ingredient_id),
                    )
                )
                if existing is None:
                    session.add(
                        ExtraIngredient(
                            extra_id=extra.id,
                            ingredient_id=int(ingredient_id),
                            quantity=_float_from_csv(_csv_key(row, "quantity"), row_num, "quantity"),
                        )
                    )
                    linked += 1
                else:
                    existing.quantity = _float_from_csv(_csv_key(row, "quantity"), row_num, "quantity")
                    updated += 1

        except Exception as exc:
            errors.append(CatalogCsvImportError(row=row_num, code="IMPORT_ERROR", detail=str(exc)))

    if errors:
        await session.rollback()
        return CatalogCsvConfirmResponse(
            token=token,
            imported=False,
            total_rows=len(rows),
            created=0,
            updated=0,
            linked=0,
            errors=errors,
        )

    batch.status = "imported"
    await session.commit()
    return CatalogCsvConfirmResponse(
        token=token,
        imported=True,
        total_rows=len(rows),
        created=created,
        updated=updated,
        linked=linked,
        errors=[],
    )


async def export_catalog_csv(session: AsyncSession) -> str:
    output = StringIO()
    fieldnames = [
        "type",
        "name",
        "product_name",
        "category_name",
        "description",
        "base_price",
        "image_url",
        "is_active",
        "variant_name",
        "price_delta",
        "extra_name",
        "price",
        "allergen_slug",
        "level",
        "dietary_slug",
        "recommended_product_name",
        "display_order",
        "label",
        "ingredient_id",
        "quantity",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    categories_result = await session.execute(select(Category).order_by(Category.display_order, Category.name))
    categories = {category.id: category for category in categories_result.scalars()}
    for category in categories.values():
        writer.writerow({
            "type": "category",
            "name": category.name,
            "display_order": category.display_order,
            "is_active": category.is_active,
        })

    products_result = await session.execute(select(Product).order_by(Product.name))
    products = list(products_result.scalars())
    products_by_id = {product.id: product for product in products}
    for product in products:
        writer.writerow({
            "type": "product",
            "name": product.name,
            "category_name": categories.get(product.category_id).name if product.category_id in categories else "",
            "description": product.description,
            "base_price": float(product.base_price),
            "image_url": product.image_url,
            "is_active": product.is_active,
        })

    variants_result = await session.execute(select(ProductVariant).order_by(ProductVariant.product_id, ProductVariant.name))
    for variant in variants_result.scalars():
        product = products_by_id.get(variant.product_id)
        writer.writerow({
            "type": "variant",
            "product_name": product.name if product else "",
            "variant_name": variant.name,
            "price_delta": float(variant.price_delta),
            "is_active": variant.is_active,
        })

    extras_result = await session.execute(select(Extra).order_by(Extra.name))
    extras = {extra.id: extra for extra in extras_result.scalars()}
    for extra in extras.values():
        writer.writerow({
            "type": "extra",
            "extra_name": extra.name,
            "price": float(extra.price),
            "is_active": extra.is_active,
        })

    links_result = await session.execute(select(ProductExtra))
    for link in links_result.scalars():
        writer.writerow({
            "type": "product_extra",
            "product_name": products_by_id.get(link.product_id).name if link.product_id in products_by_id else "",
            "extra_name": extras.get(link.extra_id).name if link.extra_id in extras else "",
        })

    recommendations_result = await session.execute(
        select(ProductRecommendation).where(ProductRecommendation.is_active.is_(True)).order_by(ProductRecommendation.product_id)
    )
    for recommendation in recommendations_result.scalars():
        writer.writerow({
            "type": "recommendation",
            "product_name": products_by_id.get(recommendation.product_id).name if recommendation.product_id in products_by_id else "",
            "recommended_product_name": products_by_id.get(recommendation.recommended_product_id).name if recommendation.recommended_product_id in products_by_id else "",
            "display_order": recommendation.display_order,
            "label": recommendation.label,
        })

    extra_ingredients_result = await session.execute(select(ExtraIngredient).order_by(ExtraIngredient.extra_id))
    for item in extra_ingredients_result.scalars():
        writer.writerow({
            "type": "extra_ingredient",
            "extra_name": extras.get(item.extra_id).name if item.extra_id in extras else "",
            "ingredient_id": item.ingredient_id,
            "quantity": float(item.quantity),
        })

    return output.getvalue()
