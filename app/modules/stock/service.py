from datetime import datetime

from arq import ArqRedis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http.errors import AppError
from app.core.http.schemas import PaginationParams
from app.modules.catalog.models import ExtraIngredient, Product
from app.modules.orders.models import OrderItem
from app.modules.stock.models import Ingredient, ProductIngredient, StockMovement, VariantIngredient


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
