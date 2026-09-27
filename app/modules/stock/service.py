from datetime import datetime, timedelta, timezone

from arq import ArqRedis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.admin.tenants.models import TenantConfig
from app.modules.catalog.models import (
    Extra,
    ExtraIngredient,
    Product,
    ProductAvailabilityOverride,
    ProductExtra,
    ProductVariant,
)
from app.modules.orders.models import OrderItem
from app.modules.stock.models import (
    Ingredient,
    IngredientBatch,
    ProductIngredient,
    StockAdjustmentRequest,
    StockMovement,
    StockRecipeAuditLog,
    VariantIngredient,
)
from app.modules.stock.schemas import StockRecipeLineCreate, StockRecipeReplace

_UNIT_FACTORS: dict[str, tuple[str, float]] = {
    "g": ("mass", 1.0),
    "gram": ("mass", 1.0),
    "grams": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "kilogram": ("mass", 1000.0),
    "kilograms": ("mass", 1000.0),
    "ml": ("volume", 1.0),
    "milliliter": ("volume", 1.0),
    "milliliters": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "liter": ("volume", 1000.0),
    "liters": ("volume", 1000.0),
    "piece": ("count", 1.0),
    "pieces": ("count", 1.0),
    "pc": ("count", 1.0),
    "pcs": ("count", 1.0),
    "unit": ("count", 1.0),
    "units": ("count", 1.0),
    "unite": ("count", 1.0),
    "unites": ("count", 1.0),
    "portion": ("count", 1.0),
    "portions": ("count", 1.0),
}

_AUTO_STOCK_USER_ID = 0
_AUTO_STOCK_REASON_PREFIX = "Stock insuffisant"


def _stock_unavailable_reason(limiting_ingredient: str | None) -> str:
    if limiting_ingredient:
        return f"{_AUTO_STOCK_REASON_PREFIX} : {limiting_ingredient}"
    return _AUTO_STOCK_REASON_PREFIX


def _clean_unit(unit: str) -> str:
    return unit.strip().lower()


def _normalize_quantity(
    quantity: float,
    *,
    from_unit: str | None,
    ingredient: Ingredient,
) -> tuple[float, str]:
    target_unit = _clean_unit(ingredient.unit)
    source_unit = _clean_unit(from_unit or ingredient.unit)
    if source_unit == target_unit:
        return float(quantity), ingredient.unit

    source = _UNIT_FACTORS.get(source_unit)
    target = _UNIT_FACTORS.get(target_unit)
    if source is None or target is None or source[0] != target[0]:
        raise AppError(
            "INVALID_RECIPE_UNIT",
            "Recipe unit is not compatible with ingredient stock unit",
            422,
            "unit",
        )

    normalized = float(quantity) * source[1] / target[1]
    return normalized, ingredient.unit


def _effective_batch_expires_at(batch: IngredientBatch) -> datetime | None:
    opened_at = getattr(batch, "opened_at", None)
    use_within = getattr(batch, "use_within_hours_after_opening", None)
    after_open = opened_at + timedelta(hours=int(use_within)) if opened_at and use_within else None
    expires_at = getattr(batch, "expires_at", None)
    if after_open and expires_at:
        return min(after_open, expires_at)
    return after_open or expires_at


def _batch_payload(batch: IngredientBatch) -> dict:
    return {
        "id": batch.id,
        "ingredient_id": batch.ingredient_id,
        "quantity": float(batch.quantity),
        "received_at": batch.received_at,
        "expires_at": batch.expires_at,
        "opened_at": batch.opened_at,
        "use_within_hours_after_opening": batch.use_within_hours_after_opening,
        "effective_expires_at": _effective_batch_expires_at(batch),
        "status": batch.status,
        "created_by_user_id": batch.created_by_user_id,
        "created_at": batch.created_at,
    }


def _adjustment_request_payload(request: StockAdjustmentRequest, is_large_adjustment: bool) -> dict:
    return {
        "id": request.id,
        "ingredient_id": request.ingredient_id,
        "quantity_delta": float(request.quantity_delta),
        "reason": request.reason,
        "note": request.note,
        "status": request.status,
        "requested_by_user_id": request.requested_by_user_id,
        "reviewed_by_user_id": request.reviewed_by_user_id,
        "reviewed_at": request.reviewed_at,
        "is_large_adjustment": is_large_adjustment,
        "created_at": request.created_at,
    }


def _recipe_payload(recipe, recipe_type: str, target_id: int, ingredient: Ingredient | None = None) -> dict:
    return {
        "id": recipe.id,
        "recipe_type": recipe_type,
        "target_id": target_id,
        "ingredient_id": recipe.ingredient_id,
        "ingredient_name": ingredient.name if ingredient is not None else None,
        "quantity": float(recipe.quantity),
        "unit": recipe.quantity_unit or (ingredient.unit if ingredient is not None else None),
    }


async def _recipe_ingredients_by_id(session: AsyncSession, recipes: list) -> dict[int, Ingredient]:
    ingredient_ids = {recipe.ingredient_id for recipe in recipes}
    if not ingredient_ids:
        return {}
    result = await session.execute(select(Ingredient).where(Ingredient.id.in_(ingredient_ids)))
    return {ingredient.id: ingredient for ingredient in result.scalars()}


def _recipe_response(recipe_type: str, target_id: int, recipes: list, ingredients: dict[int, Ingredient]) -> dict:
    return {
        "recipe_type": recipe_type,
        "target_id": target_id,
        "items": [
            _recipe_payload(recipe, recipe_type, target_id, ingredients.get(recipe.ingredient_id))
            for recipe in recipes
        ],
    }


def _recipe_audit_items(
    recipe_type: str,
    target_id: int,
    recipes: list,
    ingredients: dict[int, Ingredient],
) -> list[dict]:
    return sorted(
        [
            _recipe_payload(recipe, recipe_type, target_id, ingredients.get(recipe.ingredient_id))
            for recipe in recipes
        ],
        key=lambda item: (item["ingredient_id"], item["id"] or 0),
    )


def _add_recipe_audit_log(
    session: AsyncSession,
    *,
    recipe_type: str,
    target_id: int,
    changed_by_user_id: int | None,
    old_items: list[dict],
    new_items: list[dict],
) -> None:
    if old_items == new_items:
        return
    session.add(
        StockRecipeAuditLog(
            recipe_type=recipe_type,
            target_id=target_id,
            changed_by_user_id=changed_by_user_id,
            old_items=old_items,
            new_items=new_items,
        )
    )


def _recipe_config(recipe_type: str):
    if recipe_type == "product":
        return ProductIngredient, ProductIngredient.product_id, Product, "PRODUCT_NOT_FOUND", "Product not found"
    if recipe_type == "variant":
        return (
            VariantIngredient,
            VariantIngredient.variant_id,
            ProductVariant,
            "VARIANT_NOT_FOUND",
            "Product variant not found",
        )
    if recipe_type == "extra":
        return ExtraIngredient, ExtraIngredient.extra_id, Extra, "EXTRA_NOT_FOUND", "Extra not found"
    raise AppError("INVALID_RECIPE_TYPE", "Invalid recipe type", 422, "recipe_type")


async def _ensure_recipe_target(session: AsyncSession, recipe_type: str, target_id: int) -> None:
    _, _, target_model, error_code, error_detail = _recipe_config(recipe_type)
    if await session.get(target_model, target_id) is None:
        raise AppError(error_code, error_detail, 404)


async def _validate_recipe_items(
    session: AsyncSession,
    items: list[StockRecipeLineCreate],
) -> dict[int, Ingredient]:
    seen: set[int] = set()
    duplicates: set[int] = set()
    for item in items:
        if item.ingredient_id in seen:
            duplicates.add(item.ingredient_id)
        seen.add(item.ingredient_id)
    if duplicates:
        raise AppError(
            "DUPLICATE_RECIPE_INGREDIENT",
            "Recipe cannot contain the same ingredient twice",
            409,
            "ingredient_id",
        )

    if not seen:
        return {}

    result = await session.execute(select(Ingredient).where(Ingredient.id.in_(seen)))
    ingredients = {ingredient.id: ingredient for ingredient in result.scalars()}
    missing = seen - set(ingredients)
    if missing:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404, "ingredient_id")
    return ingredients


async def _ensure_unique_ingredient_name(
    session: AsyncSession,
    name: str,
    *,
    exclude_ingredient_id: int | None = None,
) -> str:
    normalized_name = " ".join(name.strip().split())
    if not normalized_name:
        raise AppError("INVALID_INGREDIENT_NAME", "Ingredient name is required", 422, "name")

    query = select(Ingredient).where(func.lower(func.trim(Ingredient.name)) == normalized_name.lower())
    if exclude_ingredient_id is not None:
        query = query.where(Ingredient.id != exclude_ingredient_id)
    existing = await session.scalar(query)
    if existing is not None:
        raise AppError(
            "INGREDIENT_ALREADY_EXISTS",
            "An ingredient with this name already exists",
            409,
            "name",
        )
    return normalized_name


async def _large_adjustment_threshold(session: AsyncSession) -> float:
    config = await session.scalar(select(TenantConfig))
    if config is None:
        return 10.0
    return float(getattr(config, "large_stock_adjustment_threshold", 10) or 0)


async def _is_large_adjustment(session: AsyncSession, quantity_delta: float) -> bool:
    threshold = await _large_adjustment_threshold(session)
    return threshold > 0 and abs(float(quantity_delta)) >= threshold


async def list_ingredients(
    session: AsyncSession,
    pagination: PaginationParams,
    below_threshold: bool | None = None,
    unit: str | None = None,
    search: str | None = None,
) -> tuple[list[Ingredient], int]:
    """Retourne une page d'ingredients tries par nom.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        pagination: Parametres de pagination (page, page_size).

    Returns:
        Tuple (liste des ingredients de la page, total toutes pages confondues).
    """
    filters = []
    if below_threshold is True:
        filters.append(Ingredient.current_qty < Ingredient.alert_threshold)
    elif below_threshold is False:
        filters.append(Ingredient.current_qty >= Ingredient.alert_threshold)

    if unit:
        filters.append(Ingredient.unit == unit)

    if search:
        filters.append(Ingredient.name.ilike(f"%{search}%"))

    base_query = select(Ingredient)
    count_query = select(func.count()).select_from(Ingredient)
    if filters:
        base_query = base_query.where(*filters)
        count_query = count_query.where(*filters)

    total = await session.scalar(count_query) or 0
    result = await session.execute(
        base_query
        .order_by(Ingredient.name)
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


async def list_movements(
    session: AsyncSession,
    pagination: PaginationParams,
    ingredient_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[StockMovement], int]:
    filters = []
    if ingredient_id is not None:
        filters.append(StockMovement.ingredient_id == ingredient_id)
    if date_from is not None:
        filters.append(StockMovement.created_at >= date_from)
    if date_to is not None:
        filters.append(StockMovement.created_at <= date_to)

    base_query = select(StockMovement)
    count_query = select(func.count()).select_from(StockMovement)
    if filters:
        base_query = base_query.where(*filters)
        count_query = count_query.where(*filters)

    total = await session.scalar(count_query) or 0
    result = await session.execute(
        base_query
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    return list(result.scalars()), total


async def list_alerts(session: AsyncSession) -> list[Ingredient]:
    result = await session.execute(
        select(Ingredient)
        .where(Ingredient.current_qty < Ingredient.alert_threshold)
        .order_by(Ingredient.current_qty.asc(), Ingredient.name.asc())
    )
    return list(result.scalars())


async def create_ingredient(
    session: AsyncSession,
    data: dict,
) -> Ingredient:
    data = dict(data)
    data["name"] = await _ensure_unique_ingredient_name(session, str(data.get("name") or ""))
    ingredient = Ingredient(**data)
    session.add(ingredient)
    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def get_recipe(session: AsyncSession, recipe_type: str, target_id: int) -> dict:
    await _ensure_recipe_target(session, recipe_type, target_id)
    recipe_model, target_column, _, _, _ = _recipe_config(recipe_type)
    result = await session.execute(
        select(recipe_model).where(target_column == target_id).order_by(recipe_model.id)
    )
    recipes = list(result.scalars())
    ingredients = await _recipe_ingredients_by_id(session, recipes)
    return _recipe_response(recipe_type, target_id, recipes, ingredients)


async def replace_recipe(
    session: AsyncSession,
    recipe_type: str,
    target_id: int,
    body: StockRecipeReplace,
    user_id: int | None = None,
) -> dict:
    await _ensure_recipe_target(session, recipe_type, target_id)
    ingredients = await _validate_recipe_items(session, body.items)
    recipe_model, target_column, _, _, _ = _recipe_config(recipe_type)
    target_field = target_column.key
    existing_result = await session.execute(
        select(recipe_model).where(target_column == target_id).order_by(recipe_model.id)
    )
    existing_recipes = list(existing_result.scalars())
    existing_ingredients = await _recipe_ingredients_by_id(session, existing_recipes)
    old_items = _recipe_audit_items(recipe_type, target_id, existing_recipes, existing_ingredients)

    await session.execute(delete(recipe_model).where(target_column == target_id))
    recipes = []
    for item in body.items:
        normalized_quantity, normalized_unit = _normalize_quantity(
            item.quantity,
            from_unit=item.unit,
            ingredient=ingredients[item.ingredient_id],
        )
        recipes.append(
            recipe_model(
                **{
                    target_field: target_id,
                    "ingredient_id": item.ingredient_id,
                    "quantity": normalized_quantity,
                    "quantity_unit": normalized_unit,
                }
            )
        )
    for recipe in recipes:
        session.add(recipe)

    await session.flush()
    new_items = _recipe_audit_items(recipe_type, target_id, recipes, ingredients)
    _add_recipe_audit_log(
        session,
        recipe_type=recipe_type,
        target_id=target_id,
        changed_by_user_id=user_id,
        old_items=old_items,
        new_items=new_items,
    )
    await sync_auto_stock_availability_overrides(
        session,
        await _product_ids_for_recipe_target(session, recipe_type, target_id),
    )
    await session.commit()
    return _recipe_response(recipe_type, target_id, recipes, ingredients)


async def create_recipe_line(
    session: AsyncSession,
    recipe_type: str,
    target_id: int,
    item: StockRecipeLineCreate,
    user_id: int | None = None,
) -> dict:
    await _ensure_recipe_target(session, recipe_type, target_id)
    ingredients = await _validate_recipe_items(session, [item])
    recipe_model, target_column, _, _, _ = _recipe_config(recipe_type)
    target_field = target_column.key
    existing_result = await session.execute(
        select(recipe_model).where(target_column == target_id).order_by(recipe_model.id)
    )
    existing_recipes = list(existing_result.scalars())
    existing_ingredients = await _recipe_ingredients_by_id(session, existing_recipes)
    old_items = _recipe_audit_items(recipe_type, target_id, existing_recipes, existing_ingredients)

    existing = await session.scalar(
        select(recipe_model).where(
            target_column == target_id,
            recipe_model.ingredient_id == item.ingredient_id,
        )
    )
    if existing is not None:
        raise AppError(
            "DUPLICATE_RECIPE_INGREDIENT",
            "Recipe already contains this ingredient",
            409,
            "ingredient_id",
        )

    normalized_quantity, normalized_unit = _normalize_quantity(
        item.quantity,
        from_unit=item.unit,
        ingredient=ingredients[item.ingredient_id],
    )
    recipe = recipe_model(
        **{
            target_field: target_id,
            "ingredient_id": item.ingredient_id,
            "quantity": normalized_quantity,
            "quantity_unit": normalized_unit,
        }
    )
    session.add(recipe)
    await session.flush()
    new_items = _recipe_audit_items(
        recipe_type,
        target_id,
        [*existing_recipes, recipe],
        {**existing_ingredients, **ingredients},
    )
    _add_recipe_audit_log(
        session,
        recipe_type=recipe_type,
        target_id=target_id,
        changed_by_user_id=user_id,
        old_items=old_items,
        new_items=new_items,
    )
    await sync_auto_stock_availability_overrides(
        session,
        await _product_ids_for_recipe_target(session, recipe_type, target_id),
    )
    await session.commit()
    return _recipe_payload(recipe, recipe_type, target_id, ingredients.get(item.ingredient_id))


async def delete_recipe_line(
    session: AsyncSession,
    recipe_type: str,
    recipe_line_id: int,
    user_id: int | None = None,
) -> None:
    recipe_model, target_column, _, _, _ = _recipe_config(recipe_type)
    recipe = await session.get(recipe_model, recipe_line_id)
    if recipe is None:
        raise AppError("RECIPE_LINE_NOT_FOUND", "Recipe line not found", 404)
    target_id = getattr(recipe, target_column.key)
    existing_result = await session.execute(
        select(recipe_model).where(target_column == target_id).order_by(recipe_model.id)
    )
    existing_recipes = list(existing_result.scalars())
    existing_ingredients = await _recipe_ingredients_by_id(session, existing_recipes)
    old_items = _recipe_audit_items(recipe_type, target_id, existing_recipes, existing_ingredients)
    new_recipes = [
        item
        for item in existing_recipes
        if getattr(item, "id", None) != recipe_line_id
    ]
    new_items = _recipe_audit_items(recipe_type, target_id, new_recipes, existing_ingredients)
    await session.delete(recipe)
    _add_recipe_audit_log(
        session,
        recipe_type=recipe_type,
        target_id=target_id,
        changed_by_user_id=user_id,
        old_items=old_items,
        new_items=new_items,
    )
    await sync_auto_stock_availability_overrides(
        session,
        await _product_ids_for_recipe_target(session, recipe_type, target_id),
    )
    await session.commit()


async def list_missing_recipes(session: AsyncSession) -> list[dict]:
    missing: list[dict] = []

    product_recipe_ids = set(
        (await session.execute(select(ProductIngredient.product_id).distinct())).scalars()
    )
    products = await session.execute(select(Product).where(Product.is_active.is_(True)).order_by(Product.name))
    for product in products.scalars():
        if product.id not in product_recipe_ids:
            missing.append(
                {
                    "recipe_type": "product",
                    "target_id": product.id,
                    "name": product.name,
                    "product_id": product.id,
                }
            )

    variant_recipe_ids = set(
        (await session.execute(select(VariantIngredient.variant_id).distinct())).scalars()
    )
    variants = await session.execute(
        select(ProductVariant).where(ProductVariant.is_active.is_(True)).order_by(ProductVariant.name)
    )
    for variant in variants.scalars():
        if variant.id not in variant_recipe_ids:
            missing.append(
                {
                    "recipe_type": "variant",
                    "target_id": variant.id,
                    "name": variant.name,
                    "product_id": variant.product_id,
                }
            )

    extra_recipe_ids = set(
        (await session.execute(select(ExtraIngredient.extra_id).distinct())).scalars()
    )
    extras = await session.execute(select(Extra).where(Extra.is_active.is_(True)).order_by(Extra.name))
    for extra in extras.scalars():
        if extra.id not in extra_recipe_ids:
            missing.append(
                {
                    "recipe_type": "extra",
                    "target_id": extra.id,
                    "name": extra.name,
                    "product_id": None,
                }
            )

    return missing


async def _latest_availability_overrides(
    session: AsyncSession,
    product_ids: set[int],
) -> dict[int, ProductAvailabilityOverride]:
    if not product_ids:
        return {}
    result = await session.execute(
        select(ProductAvailabilityOverride)
        .where(ProductAvailabilityOverride.product_id.in_(tuple(product_ids)))
        .order_by(
            ProductAvailabilityOverride.product_id,
            ProductAvailabilityOverride.created_at.desc(),
            ProductAvailabilityOverride.id.desc(),
        )
    )
    latest: dict[int, ProductAvailabilityOverride] = {}
    for override in result.scalars():
        latest.setdefault(override.product_id, override)
    return latest


async def _product_ids_for_ingredients(
    session: AsyncSession,
    ingredient_ids: set[int],
) -> set[int]:
    if not ingredient_ids:
        return set()

    product_ids: set[int] = set(
        (
            await session.execute(
                select(ProductIngredient.product_id)
                .where(ProductIngredient.ingredient_id.in_(tuple(ingredient_ids)))
                .distinct()
            )
        ).scalars()
    )

    variant_product_ids = await session.execute(
        select(ProductVariant.product_id)
        .join(VariantIngredient, VariantIngredient.variant_id == ProductVariant.id)
        .where(VariantIngredient.ingredient_id.in_(tuple(ingredient_ids)))
        .distinct()
    )
    product_ids.update(variant_product_ids.scalars())

    extra_product_ids = await session.execute(
        select(ProductExtra.product_id)
        .join(ExtraIngredient, ExtraIngredient.extra_id == ProductExtra.extra_id)
        .where(ExtraIngredient.ingredient_id.in_(tuple(ingredient_ids)))
        .distinct()
    )
    product_ids.update(extra_product_ids.scalars())
    return product_ids


async def _product_ids_for_recipe_target(
    session: AsyncSession,
    recipe_type: str,
    target_id: int,
) -> set[int]:
    if recipe_type == "product":
        return {target_id}
    if recipe_type == "variant":
        variant = await session.get(ProductVariant, target_id)
        return {variant.product_id} if variant is not None else set()
    if recipe_type == "extra":
        result = await session.execute(
            select(ProductExtra.product_id).where(ProductExtra.extra_id == target_id)
        )
        return set(result.scalars())
    return set()


async def sync_auto_stock_availability_overrides(
    session: AsyncSession,
    product_ids: set[int] | None = None,
) -> list[ProductAvailabilityOverride]:
    """Cree des indisponibilites systeme pour les produits non produisibles.

    Ne cree jamais d'override available=true : le retour en stock doit rester
    une validation admin explicite.
    """
    if product_ids is None:
        product_ids = set(
            (
                await session.execute(
                    select(Product.id).where(Product.is_active.is_(True))
                )
            ).scalars()
        )
    else:
        product_ids = {int(product_id) for product_id in product_ids if product_id}
    if not product_ids:
        return []

    availability = await get_products_availability(session, sorted(product_ids))
    unavailable_ids = {
        product_id
        for product_id, item in availability.items()
        if item.get("available") is False
    }
    if not unavailable_ids:
        return []

    latest_overrides = await _latest_availability_overrides(session, unavailable_ids)
    created: list[ProductAvailabilityOverride] = []
    for product_id in sorted(unavailable_ids):
        latest = latest_overrides.get(product_id)
        if latest is not None and latest.available is False:
            continue
        item = availability[product_id]
        override = ProductAvailabilityOverride(
            product_id=product_id,
            available=False,
            reason=_stock_unavailable_reason(item.get("limiting_ingredient")),
            changed_by_user_id=_AUTO_STOCK_USER_ID,
        )
        session.add(override)
        created.append(override)

    if created:
        await session.flush()
    return created


async def supply(
    session: AsyncSession,
    ingredient_id: int,
    quantity: float,
    user_id: int | None = None,
) -> Ingredient:
    """Approvisionne un ingredient (ajoute du stock).

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        ingredient_id: Cle primaire de l'ingredient.
        quantity: Quantite a ajouter (doit etre positive).
        user_id: Cle primaire de l'utilisateur authentifie qui effectue l'ajout.

    Returns:
        Instance Ingredient mise a jour.

    Raises:
        AppError: INGREDIENT_NOT_FOUND (404) si l'ingredient est introuvable.
    """
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)
    ingredient.current_qty = float(ingredient.current_qty) + quantity
    session.add(
        StockMovement(
            ingredient_id=ingredient.id,
            quantity_delta=quantity,
            reason="supply",
            user_id=user_id,
        )
    )
    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def list_batches(
    session: AsyncSession,
    ingredient_id: int,
) -> list[dict]:
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)
    result = await session.execute(
        select(IngredientBatch)
        .where(IngredientBatch.ingredient_id == ingredient_id)
        .order_by(IngredientBatch.received_at.desc(), IngredientBatch.id.desc())
    )
    return [_batch_payload(batch) for batch in result.scalars()]


async def create_batch(
    session: AsyncSession,
    ingredient_id: int,
    body,
    user_id: int | None,
) -> dict:
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)

    received_at = body.received_at or datetime.now(timezone.utc)
    batch = IngredientBatch(
        ingredient_id=ingredient_id,
        quantity=body.quantity,
        received_at=received_at,
        expires_at=body.expires_at,
        use_within_hours_after_opening=body.use_within_hours_after_opening,
        status="sealed",
        created_by_user_id=user_id,
    )
    session.add(batch)
    await session.flush()
    ingredient.current_qty = float(ingredient.current_qty) + float(body.quantity)
    session.add(
        StockMovement(
            ingredient_id=ingredient_id,
            quantity_delta=float(body.quantity),
            reason=f"batch:{batch.id}",
            user_id=user_id,
        )
    )
    await session.commit()
    await session.refresh(batch)
    return _batch_payload(batch)


async def patch_batch(session: AsyncSession, batch_id: int, body) -> dict:
    batch = await session.get(IngredientBatch, batch_id)
    if batch is None:
        raise AppError("BATCH_NOT_FOUND", "Ingredient batch not found", 404)
    updates = body.model_dump(exclude_unset=True)
    if "quantity" in updates and float(updates["quantity"]) != float(batch.quantity):
        ingredient = await session.get(Ingredient, batch.ingredient_id)
        if ingredient is None:
            raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)
        delta = float(updates["quantity"]) - float(batch.quantity)
        new_qty = float(ingredient.current_qty) + delta
        if new_qty < 0:
            raise AppError("INSUFFICIENT_STOCK", "Ingredient stock cannot become negative", 409)
        ingredient.current_qty = new_qty
        session.add(
            StockMovement(
                ingredient_id=batch.ingredient_id,
                quantity_delta=delta,
                reason=f"batch_adjust:{batch.id}",
                user_id=None,
            )
        )
    for key, value in updates.items():
        setattr(batch, key, value)
    await session.commit()
    await session.refresh(batch)
    return _batch_payload(batch)


async def open_batch(
    session: AsyncSession,
    batch_id: int,
    user_id: int | None,
) -> dict:
    batch = await session.get(IngredientBatch, batch_id)
    if batch is None:
        raise AppError("BATCH_NOT_FOUND", "Ingredient batch not found", 404)
    if batch.status in {"discarded", "consumed"}:
        raise AppError("BATCH_CLOSED", "Batch cannot be opened from its current status", 409)
    if batch.opened_at is None:
        batch.opened_at = datetime.now(timezone.utc)
    batch.status = "opened"
    await session.commit()
    await session.refresh(batch)
    return _batch_payload(batch)


async def discard_batch(
    session: AsyncSession,
    batch_id: int,
    reason: str,
    user_id: int | None,
) -> dict:
    batch = await session.get(IngredientBatch, batch_id)
    if batch is None:
        raise AppError("BATCH_NOT_FOUND", "Ingredient batch not found", 404)
    if batch.status == "discarded":
        return _batch_payload(batch)
    ingredient = await session.get(Ingredient, batch.ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)
    new_qty = float(ingredient.current_qty) - float(batch.quantity)
    if new_qty < 0:
        raise AppError("INSUFFICIENT_STOCK", "Ingredient stock cannot become negative", 409)
    ingredient.current_qty = new_qty
    batch.status = "discarded"
    session.add(
        StockMovement(
            ingredient_id=batch.ingredient_id,
            quantity_delta=-float(batch.quantity),
            reason=reason,
            user_id=user_id,
        )
    )
    await session.commit()
    await session.refresh(batch)
    return _batch_payload(batch)


async def create_adjustment_request(
    session: AsyncSession,
    body,
    user_id: int,
) -> dict:
    ingredient = await session.get(Ingredient, body.ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)

    request = StockAdjustmentRequest(
        ingredient_id=body.ingredient_id,
        quantity_delta=float(body.quantity_delta),
        reason=body.reason,
        note=body.note,
        status="pending",
        requested_by_user_id=user_id,
    )
    session.add(request)
    await session.commit()
    await session.refresh(request)
    return _adjustment_request_payload(
        request,
        await _is_large_adjustment(session, float(request.quantity_delta)),
    )


async def list_adjustment_requests(
    session: AsyncSession,
    pagination: PaginationParams,
    status: str | None = None,
    ingredient_id: int | None = None,
) -> tuple[list[dict], int]:
    filters = []
    if status:
        filters.append(StockAdjustmentRequest.status == status)
    if ingredient_id is not None:
        filters.append(StockAdjustmentRequest.ingredient_id == ingredient_id)

    stmt = select(StockAdjustmentRequest)
    count_stmt = select(func.count()).select_from(StockAdjustmentRequest)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    total = await session.scalar(count_stmt) or 0
    result = await session.execute(
        stmt
        .order_by(StockAdjustmentRequest.created_at.desc(), StockAdjustmentRequest.id.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )
    requests = list(result.scalars())
    payloads = [
        _adjustment_request_payload(
            request,
            await _is_large_adjustment(session, float(request.quantity_delta)),
        )
        for request in requests
    ]
    return payloads, total


async def approve_adjustment_request(
    session: AsyncSession,
    request_id: int,
    user_id: int,
    note: str | None = None,
) -> dict:
    request = await session.get(StockAdjustmentRequest, request_id)
    if request is None:
        raise AppError("ADJUSTMENT_REQUEST_NOT_FOUND", "Stock adjustment request not found", 404)
    if request.status != "pending":
        raise AppError("ADJUSTMENT_REQUEST_CLOSED", "Stock adjustment request has already been reviewed", 409)

    ingredient = await session.get(Ingredient, request.ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)

    delta = float(request.quantity_delta)
    new_qty = float(ingredient.current_qty) + delta
    if new_qty < 0:
        raise AppError("INSUFFICIENT_STOCK", "Ingredient stock cannot become negative", 409)

    ingredient.current_qty = new_qty
    request.status = "approved"
    request.reviewed_by_user_id = user_id
    request.reviewed_at = datetime.now(timezone.utc)
    if note:
        request.note = f"{request.note}\nAdmin: {note}" if request.note else f"Admin: {note}"
    session.add(
        StockMovement(
            ingredient_id=request.ingredient_id,
            quantity_delta=delta,
            reason=f"request:{request.reason}",
            user_id=user_id,
        )
    )

    await session.commit()
    await session.refresh(request)
    return _adjustment_request_payload(
        request,
        await _is_large_adjustment(session, float(request.quantity_delta)),
    )


async def reject_adjustment_request(
    session: AsyncSession,
    request_id: int,
    user_id: int,
    note: str | None = None,
) -> dict:
    request = await session.get(StockAdjustmentRequest, request_id)
    if request is None:
        raise AppError("ADJUSTMENT_REQUEST_NOT_FOUND", "Stock adjustment request not found", 404)
    if request.status != "pending":
        raise AppError("ADJUSTMENT_REQUEST_CLOSED", "Stock adjustment request has already been reviewed", 409)

    request.status = "rejected"
    request.reviewed_by_user_id = user_id
    request.reviewed_at = datetime.now(timezone.utc)
    if note:
        request.note = f"{request.note}\nAdmin: {note}" if request.note else f"Admin: {note}"

    await session.commit()
    await session.refresh(request)
    return _adjustment_request_payload(
        request,
        await _is_large_adjustment(session, float(request.quantity_delta)),
    )


async def _item_recipe_deltas(
    session: AsyncSession,
    item: OrderItem,
) -> list[tuple[int, float]]:
    deltas: list[tuple[int, float]] = []

    recipes = await session.execute(
        select(ProductIngredient).where(ProductIngredient.product_id == item.product_id)
    )
    for recipe in recipes.scalars():
        deltas.append((recipe.ingredient_id, float(recipe.quantity) * item.quantity))

    if item.variant_id is not None:
        variant_recipes = await session.execute(
            select(VariantIngredient).where(VariantIngredient.variant_id == item.variant_id)
        )
        for recipe in variant_recipes.scalars():
            deltas.append((recipe.ingredient_id, float(recipe.quantity) * item.quantity))

    extras_snapshot = list(getattr(item, "extras_snapshot", None) or [])
    extra_quantities: dict[int, int] = {}
    for extra in extras_snapshot:
        extra_id = int(extra.get("extra_id"))
        extra_quantities[extra_id] = extra_quantities.get(extra_id, 0) + int(extra.get("quantity", 1))

    if extra_quantities:
        extra_recipes = await session.execute(
            select(ExtraIngredient).where(ExtraIngredient.extra_id.in_(tuple(extra_quantities)))
        )
        for recipe in extra_recipes.scalars():
            deltas.append(
                (
                    recipe.ingredient_id,
                    float(recipe.quantity) * item.quantity * extra_quantities.get(recipe.extra_id, 0),
                )
            )

    return deltas


async def patch_ingredient(
    session: AsyncSession,
    ingredient_id: int,
    data: dict,
) -> Ingredient:
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)

    if "name" in data and data["name"] is not None:
        data["name"] = await _ensure_unique_ingredient_name(
            session,
            str(data["name"]),
            exclude_ingredient_id=ingredient_id,
        )

    for field, value in data.items():
        setattr(ingredient, field, value)

    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def adjust_ingredient_stock(
    session: AsyncSession,
    ingredient_id: int,
    quantity: float,
    reason: str,
    user_id: int | None = None,
) -> Ingredient:
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise AppError("INGREDIENT_NOT_FOUND", "Ingredient not found", 404)

    if reason == "inventory":
        new_qty = float(quantity)
        quantity_delta = new_qty - float(ingredient.current_qty)
    else:
        quantity_delta = float(quantity)
        new_qty = float(ingredient.current_qty) + quantity_delta

    if new_qty < 0:
        raise AppError("INSUFFICIENT_STOCK", "Ingredient stock cannot become negative", 409)

    ingredient.current_qty = new_qty
    session.add(
        StockMovement(
            ingredient_id=ingredient.id,
            quantity_delta=quantity_delta,
            reason=reason,
            user_id=user_id,
        )
    )
    if quantity_delta < 0:
        await sync_auto_stock_availability_overrides(
            session,
            await _product_ids_for_ingredients(session, {ingredient.id}),
        )
    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def deduct_for_order(
    session: AsyncSession,
    order_id: int,
    tenant_slug: str = "default",
    auto_commit: bool = True,
    arq_pool: ArqRedis | None = None,
    actor_user_id: int | None = None,
) -> list[Ingredient]:
    """Deduit le stock pour tous les items d'une commande.

    [PERF] Le pool arq est injecte en parametre (singleton lifespan).
    Si arq_pool est None et auto_commit=True, les alertes stock ne sont pas enqueued.

    Args:
        session: Session SQLAlchemy async. Doit appartenir a la transaction du
            caller quand auto_commit=False.
        order_id: Cle primaire de la commande dont le stock est a deduire.
        tenant_slug: Identifiant tenant utilise pour le routage des jobs arq.
        auto_commit: Si True (defaut), commit la session et enqueue les alertes
            stock arq avant de retourner. Si False, ni commit ni enqueue.
        arq_pool: Pool arq singleton injecte depuis le lifespan.
        actor_user_id: Utilisateur (staff) ayant declenche la confirmation,
            enregistre sur le StockMovement pour l'audit trail.

    Returns:
        Liste des ingredients passes sous ou au niveau de leur seuil d'alerte.

    Raises:
        AppError: INSUFFICIENT_STOCK (409) si un ingredient n'a pas assez de stock.
    """
    items = await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    low_stock: list[Ingredient] = []
    touched_ingredient_ids: set[int] = set()
    for item in items.scalars():
        for ingredient_id, delta in await _item_recipe_deltas(session, item):
            ingredient = await session.get(Ingredient, ingredient_id)
            if ingredient is None:
                continue
            if float(ingredient.current_qty) < delta:
                raise AppError("INSUFFICIENT_STOCK", f"Not enough stock for {ingredient.name}", 409)
            ingredient.current_qty = float(ingredient.current_qty) - delta
            session.add(
                StockMovement(
                    ingredient_id=ingredient.id,
                    quantity_delta=-delta,
                    reason=f"order:{order_id}",
                    user_id=actor_user_id,
                )
            )
            touched_ingredient_ids.add(ingredient.id)
            if float(ingredient.current_qty) <= float(ingredient.alert_threshold):
                low_stock.append(ingredient)

    await sync_auto_stock_availability_overrides(
        session,
        await _product_ids_for_ingredients(session, touched_ingredient_ids),
    )

    if not auto_commit:
        return low_stock

    await session.commit()

    if arq_pool is not None and low_stock:
        try:
            for ingredient in low_stock:
                await arq_pool.enqueue_job(
                    "send_stock_alert",
                    ingredient_id=ingredient.id,
                    ingredient_name=ingredient.name,
                    current_qty=float(ingredient.current_qty),
                    tenant_slug=tenant_slug,
                )
        except Exception:
            pass

    return low_stock


async def restore_for_order(
    session: AsyncSession,
    tenant_slug: str,
    order_id: int,
    actor_user_id: int | None = None,
) -> None:
    """Restitue le stock consomme par une commande (utilise lors d'une annulation).

    Lit les OrderItems de la commande et leurs recettes (ProductIngredient),
    effectue des StockMovements positifs et incremente current_qty sur chaque
    ingredient. A appeler dans la meme transaction que le changement de statut.

    [PROD] Cette fonction ne commit pas -- le commit est a la charge du caller
    (update_status dans orders/service.py) pour garantir l'atomicite.

    Args:
        session: Session SQLAlchemy async partageant la transaction du caller.
        tenant_slug: Slug tenant (pour contexte de log eventuel).
        order_id: Cle primaire de la commande dont le stock doit etre restitue.
        actor_user_id: Utilisateur (staff/customer) ayant declenche l'annulation,
            enregistre sur le StockMovement pour l'audit trail.
    """
    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )
    for item in items_result.scalars():
        for ingredient_id, delta in await _item_recipe_deltas(session, item):
            ingredient = await session.get(Ingredient, ingredient_id)
            if ingredient is None:
                continue
            ingredient.current_qty = float(ingredient.current_qty) + delta
            session.add(
                StockMovement(
                    ingredient_id=ingredient.id,
                    quantity_delta=+delta,
                    reason=f"cancel:{order_id}",
                    user_id=actor_user_id,
                )
            )


async def get_product_availability(
    session: AsyncSession,
    product_id: int,
) -> dict:
    """Calcule si le stock est suffisant pour produire au moins 1 unite du produit.

    Lit la recette du produit (ProductIngredient) et compare les quantites
    requises au stock actuel de chaque ingredient.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        product_id: Cle primaire du produit a verifier.

    Returns:
        Dict {"product_id": int, "available": bool, "limiting_ingredient": str | None}.
        limiting_ingredient est le nom de l'ingredient bloquant (ou None si disponible).
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise AppError("PRODUCT_NOT_FOUND", "Product not found", 404)

    return (await get_products_availability(session, [product_id]))[product_id]


async def get_products_availability(
    session: AsyncSession,
    product_ids: list[int],
) -> dict[int, dict]:
    """Version batchee de get_product_availability : 2 requetes au total au lieu
    d'une requete (recette + N lookups ingredient) par produit.

    [PERF] Utilisee par le listing catalogue (build_product_summaries) pour
    eliminer le N+1 sur une page pouvant contenir jusqu'a 100 produits.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        product_ids: Liste des cles primaires produits a verifier.

    Returns:
        Dict {product_id: {"product_id": int, "available": bool, "limiting_ingredient": str | None}}.
        Un product_id sans recette (ou inconnu) est considere disponible par defaut,
        au meme titre que get_product_availability.
    """
    if not product_ids:
        return {}

    product_recipes_result = await session.execute(
        select(ProductIngredient).where(ProductIngredient.product_id.in_(product_ids))
    )
    required_by_product: dict[int, dict[int, float]] = {}
    ingredient_ids: set[int] = set()
    for recipe in product_recipes_result.scalars():
        required = required_by_product.setdefault(recipe.product_id, {})
        required[recipe.ingredient_id] = required.get(recipe.ingredient_id, 0.0) + float(recipe.quantity)
        ingredient_ids.add(recipe.ingredient_id)

    variant_recipes_result = await session.execute(
        select(ProductVariant.product_id, VariantIngredient)
        .join(VariantIngredient, VariantIngredient.variant_id == ProductVariant.id)
        .where(
            ProductVariant.product_id.in_(product_ids),
            ProductVariant.is_active.is_(True),
        )
    )
    for product_id, recipe in variant_recipes_result.all():
        required = required_by_product.setdefault(product_id, {})
        required[recipe.ingredient_id] = required.get(recipe.ingredient_id, 0.0) + float(recipe.quantity)
        ingredient_ids.add(recipe.ingredient_id)

    extra_recipes_result = await session.execute(
        select(ProductExtra.product_id, ExtraIngredient)
        .join(ExtraIngredient, ExtraIngredient.extra_id == ProductExtra.extra_id)
        .join(Extra, Extra.id == ProductExtra.extra_id)
        .where(
            ProductExtra.product_id.in_(product_ids),
            Extra.is_active.is_(True),
        )
    )
    for product_id, recipe in extra_recipes_result.all():
        required = required_by_product.setdefault(product_id, {})
        required[recipe.ingredient_id] = required.get(recipe.ingredient_id, 0.0) + float(recipe.quantity)
        ingredient_ids.add(recipe.ingredient_id)

    ingredients_by_id: dict[int, Ingredient] = {}
    if ingredient_ids:
        ingredients_result = await session.execute(
            select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
        )
        ingredients_by_id = {ingredient.id: ingredient for ingredient in ingredients_result.scalars()}

    availability: dict[int, dict] = {}
    for product_id in product_ids:
        required = required_by_product.get(product_id)
        if not required:
            availability[product_id] = {"product_id": product_id, "available": True, "limiting_ingredient": None}
            continue

        limiting_ingredient: str | None = None
        for ingredient_id, required_qty in required.items():
            ingredient = ingredients_by_id.get(ingredient_id)
            if ingredient is None:
                continue
            if float(ingredient.current_qty) < required_qty:
                limiting_ingredient = ingredient.name
                break

        availability[product_id] = {
            "product_id": product_id,
            "available": limiting_ingredient is None,
            "limiting_ingredient": limiting_ingredient,
            "reason": None if limiting_ingredient is None else _stock_unavailable_reason(limiting_ingredient),
        }
    return availability
