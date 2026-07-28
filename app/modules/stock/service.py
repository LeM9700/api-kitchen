from datetime import datetime, timedelta, timezone

from arq import ArqRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.admin.tenants.models import TenantConfig
from app.modules.catalog.models import ExtraIngredient, Product
from app.modules.orders.models import OrderItem
from app.modules.stock.models import (
    Ingredient,
    IngredientBatch,
    ProductIngredient,
    StockAdjustmentRequest,
    StockMovement,
    VariantIngredient,
)


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
            if float(ingredient.current_qty) <= float(ingredient.alert_threshold):
                low_stock.append(ingredient)

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

    recipes_result = await session.execute(
        select(ProductIngredient).where(ProductIngredient.product_id == product_id)
    )
    recipes = list(recipes_result.scalars())

    if not recipes:
        # Aucune recette definie : on considere le produit disponible par defaut.
        return {"product_id": product_id, "available": True, "limiting_ingredient": None}

    for recipe in recipes:
        ingredient = await session.get(Ingredient, recipe.ingredient_id)
        if ingredient is None:
            continue
        if float(ingredient.current_qty) < float(recipe.quantity):
            return {
                "product_id": product_id,
                "available": False,
                "limiting_ingredient": ingredient.name,
            }

    return {"product_id": product_id, "available": True, "limiting_ingredient": None}


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

    recipes_result = await session.execute(
        select(ProductIngredient).where(ProductIngredient.product_id.in_(product_ids))
    )
    recipes_by_product: dict[int, list[ProductIngredient]] = {}
    ingredient_ids: set[int] = set()
    for recipe in recipes_result.scalars():
        recipes_by_product.setdefault(recipe.product_id, []).append(recipe)
        ingredient_ids.add(recipe.ingredient_id)

    ingredients_by_id: dict[int, Ingredient] = {}
    if ingredient_ids:
        ingredients_result = await session.execute(
            select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
        )
        ingredients_by_id = {ingredient.id: ingredient for ingredient in ingredients_result.scalars()}

    availability: dict[int, dict] = {}
    for product_id in product_ids:
        recipes = recipes_by_product.get(product_id)
        if not recipes:
            availability[product_id] = {"product_id": product_id, "available": True, "limiting_ingredient": None}
            continue

        limiting_ingredient: str | None = None
        for recipe in recipes:
            ingredient = ingredients_by_id.get(recipe.ingredient_id)
            if ingredient is None:
                continue
            if float(ingredient.current_qty) < float(recipe.quantity):
                limiting_ingredient = ingredient.name
                break

        availability[product_id] = {
            "product_id": product_id,
            "available": limiting_ingredient is None,
            "limiting_ingredient": limiting_ingredient,
        }
    return availability
