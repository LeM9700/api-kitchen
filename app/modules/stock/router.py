from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request

from app.core.database import get_tenant_session
from app.core.http.deps import get_pagination, require_permission, require_role
from app.core.http.limiter import limiter
from app.core.http.schemas import PaginatedResponse, PaginationParams
from app.modules.stock import service
from app.modules.stock.models import Ingredient, ProductIngredient, VariantIngredient, ExtraIngredient
from app.modules.stock.schemas import (
    ExtraIngredientCreate,
    IngredientAdjustRequest,
    IngredientBatchCreate,
    IngredientBatchDiscardRequest,
    IngredientBatchOut,
    IngredientBatchPatch,
    IngredientCreate,
    IngredientPatch,
    IngredientOut,
    ProductIngredientCreate,
    StockAdjustmentRequestCreate,
    StockAdjustmentRequestOut,
    StockAdjustmentReviewRequest,
    StockMovementOut,
    SupplyRequest,
    VariantIngredientCreate,
)

router = APIRouter()


@router.get("/ingredients", response_model=PaginatedResponse[IngredientOut])
async def ingredients(
    current_user=Depends(require_permission("stock:read", "staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
    below_threshold: bool | None = Query(default=None),
    unit: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_ingredients(
            session,
            pagination,
            below_threshold=below_threshold,
            unit=unit,
            search=search,
        )
    return PaginatedResponse.build(items, total, pagination)


@router.get("/movements", response_model=PaginatedResponse[StockMovementOut])
async def movements(
    current_user=Depends(require_permission("stock:read", "staff", "admin")),
    pagination: PaginationParams = Depends(get_pagination),
    ingredient_id: int | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_movements(
            session,
            pagination,
            ingredient_id=ingredient_id,
            date_from=date_from,
            date_to=date_to,
        )
    return PaginatedResponse.build(items, total, pagination)


@router.get("/alerts", response_model=list[IngredientOut])
async def alerts(
    current_user=Depends(require_permission("stock:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_alerts(session)


@router.post("/ingredients", response_model=IngredientOut, status_code=201)
async def create_ingredient(
    body: IngredientCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        ingredient = Ingredient(**body.model_dump())
        session.add(ingredient)
        await session.commit()
        await session.refresh(ingredient)
        return ingredient


@router.patch("/ingredients/{ingredient_id}", response_model=IngredientOut)
async def patch_ingredient(
    ingredient_id: int,
    body: IngredientPatch,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        updates = body.model_dump(exclude_none=True)
        return await service.patch_ingredient(session, ingredient_id, updates)


@router.post("/ingredients/{ingredient_id}/adjust", response_model=IngredientOut)
async def adjust_ingredient(
    ingredient_id: int,
    body: IngredientAdjustRequest,
    current_user=Depends(require_role("admin")),
):
    quantity = body.new_qty if body.reason == "inventory" else body.quantity
    assert quantity is not None

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.adjust_ingredient_stock(
            session,
            ingredient_id,
            float(quantity),
            body.reason,
            user_id=int(current_user["id"]),
        )


@router.post("/supply", response_model=IngredientOut)
async def supply(
    body: SupplyRequest,
    current_user=Depends(require_permission("stock:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.supply(
            session,
            body.ingredient_id,
            body.quantity,
            user_id=int(current_user["id"]),
        )


@router.get("/ingredients/{ingredient_id}/batches", response_model=list[IngredientBatchOut])
async def ingredient_batches(
    ingredient_id: int,
    current_user=Depends(require_permission("stock:read", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_batches(session, ingredient_id)


@router.post("/ingredients/{ingredient_id}/batches", response_model=IngredientBatchOut, status_code=201)
async def create_ingredient_batch(
    ingredient_id: int,
    body: IngredientBatchCreate,
    current_user=Depends(require_permission("stock:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_batch(
            session,
            ingredient_id,
            body,
            user_id=int(current_user["id"]),
        )


@router.patch("/batches/{batch_id}", response_model=IngredientBatchOut)
async def patch_ingredient_batch(
    batch_id: int,
    body: IngredientBatchPatch,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.patch_batch(session, batch_id, body)


@router.post("/batches/{batch_id}/open", response_model=IngredientBatchOut)
async def open_ingredient_batch(
    batch_id: int,
    current_user=Depends(require_permission("stock:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.open_batch(session, batch_id, user_id=int(current_user["id"]))


@router.post("/batches/{batch_id}/discard", response_model=IngredientBatchOut)
async def discard_ingredient_batch(
    batch_id: int,
    body: IngredientBatchDiscardRequest,
    current_user=Depends(require_permission("stock:write", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.discard_batch(
            session,
            batch_id,
            body.reason,
            user_id=int(current_user["id"]),
        )


@router.post(
    "/adjustment-requests",
    response_model=StockAdjustmentRequestOut,
    status_code=201,
)
async def create_adjustment_request(
    body: StockAdjustmentRequestCreate,
    current_user=Depends(require_permission("stock:adjustment:create", "staff", "admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_adjustment_request(
            session,
            body,
            user_id=int(current_user["id"]),
        )


@router.get("/adjustment-requests", response_model=PaginatedResponse[StockAdjustmentRequestOut])
async def adjustment_requests(
    current_user=Depends(require_role("admin")),
    pagination: PaginationParams = Depends(get_pagination),
    status: str | None = Query(default=None),
    ingredient_id: int | None = Query(default=None),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_adjustment_requests(
            session,
            pagination,
            status=status,
            ingredient_id=ingredient_id,
        )
    return PaginatedResponse.build(items, total, pagination)


@router.post("/adjustment-requests/{request_id}/approve", response_model=StockAdjustmentRequestOut)
async def approve_adjustment_request(
    request_id: int,
    body: StockAdjustmentReviewRequest,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.approve_adjustment_request(
            session,
            request_id,
            user_id=int(current_user["id"]),
            note=body.note,
        )


@router.post("/adjustment-requests/{request_id}/reject", response_model=StockAdjustmentRequestOut)
async def reject_adjustment_request(
    request_id: int,
    body: StockAdjustmentReviewRequest,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.reject_adjustment_request(
            session,
            request_id,
            user_id=int(current_user["id"]),
            note=body.note,
        )


@router.post("/recipes", status_code=201)
async def create_recipe(
    body: ProductIngredientCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        recipe = ProductIngredient(**body.model_dump())
        session.add(recipe)
        await session.commit()
        return {"id": recipe.id}


@router.post("/recipes/variant", status_code=201)
async def create_variant_recipe(
    body: VariantIngredientCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        recipe = VariantIngredient(**body.model_dump())
        session.add(recipe)
        await session.commit()
        return {"id": recipe.id}


@router.post("/recipes/extra", status_code=201)
async def create_extra_recipe(
    body: ExtraIngredientCreate,
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        recipe = ExtraIngredient(**body.model_dump())
        session.add(recipe)
        await session.commit()
        return {"id": recipe.id}


@router.get("/availability")
@limiter.limit("60/minute")
async def availability(
    request: Request,
    product_ids: list[int] = Query(..., description="Liste des product_id a verifier"),
    current_user=Depends(require_permission("stock:read", "staff", "admin")),
):
    """Verifie la disponibilite stock pour une liste de produits.

    Pour chaque product_id, calcule si le stock est suffisant pour produire
    au moins 1 unite (lecture recette + stock courant de chaque ingredient).

    Args:
        product_ids: Liste d'identifiants produits (query param repete ou CSV).
        current_user: Utilisateur staff ou admin du tenant.

    Returns:
        Liste de {"product_id": int, "available": bool, "limiting_ingredient": str | None}.
    """
    results = []
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        for pid in product_ids:
            result = await service.get_product_availability(session, pid)
            results.append(result)
    return results
