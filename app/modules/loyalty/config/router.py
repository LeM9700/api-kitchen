"""Router de fidélité configurable.

Expose la gestion admin de la configuration, des règles et des paliers
de récompense, ainsi que les endpoints clients (preview, échange).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.core.database import get_tenant_session
from app.core.http.deps import get_current_user, require_role
from app.core.http.errors import AppError
from app.modules.loyalty.config import service as config_service
from app.modules.loyalty.config.models import LoyaltyConfig, LoyaltyReward, LoyaltyRule
from app.modules.loyalty.config.schemas import (
    LoyaltyConfigResponse,
    LoyaltyConfigUpdate,
    LoyaltyPointsPreview,
    LoyaltyRewardCreate,
    LoyaltyRewardEligibilityResponse,
    LoyaltyRewardResponse,
    LoyaltyRewardUpdate,
    LoyaltyRuleCreate,
    LoyaltyRuleResponse,
    LoyaltyRuleUpdate,
    LoyaltyStatsResponse,
    RedeemResponse,
)
from app.modules.loyalty.account.service import get_or_create_account

router = APIRouter()


# ---------------------------------------------------------------------------
# Configuration globale (admin)
# ---------------------------------------------------------------------------


@router.get("/config", response_model=LoyaltyConfigResponse)
async def get_config(current_user=Depends(require_role("admin"))):
    """Retourne la configuration de fidélité du tenant.

    Args:
        current_user: Utilisateur admin authentifié (JWT).

    Returns:
        Configuration active ou créée avec les valeurs par défaut.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        config = await config_service.get_or_create_loyalty_config(session)
        await session.commit()
        return LoyaltyConfigResponse.model_validate(config)


@router.patch("/config", response_model=LoyaltyConfigResponse)
async def update_config(body: LoyaltyConfigUpdate, current_user=Depends(require_role("admin"))):
    """Met à jour la configuration de fidélité du tenant.

    Args:
        body: Champs à mettre à jour (tous optionnels).
        current_user: Utilisateur admin authentifié.

    Returns:
        Configuration mise à jour.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        config = await config_service.get_or_create_loyalty_config(session)
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(config, field, value)
        await session.commit()
        await session.refresh(config)
        return LoyaltyConfigResponse.model_validate(config)


# ---------------------------------------------------------------------------
# Règles de bonus (admin)
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=list[LoyaltyRuleResponse])
async def list_rules(current_user=Depends(require_role("admin"))):
    """Liste toutes les règles de bonus du tenant (actives et inactives).

    Args:
        current_user: Utilisateur admin authentifié.

    Returns:
        Liste des règles triées par priorité croissante.
    """
    from sqlalchemy import select

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        result = await session.execute(select(LoyaltyRule).order_by(LoyaltyRule.priority.asc()))
        rules = list(result.scalars())
        return [LoyaltyRuleResponse.model_validate(r) for r in rules]


@router.post("/rules", response_model=LoyaltyRuleResponse, status_code=201)
async def create_rule(body: LoyaltyRuleCreate, current_user=Depends(require_role("admin"))):
    """Crée une nouvelle règle de bonus.

    Args:
        body: Paramètres de la règle.
        current_user: Utilisateur admin authentifié.

    Returns:
        Règle créée.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        rule = LoyaltyRule(**body.model_dump())
        config_service.validate_rule_state(rule)
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return LoyaltyRuleResponse.model_validate(rule)


@router.patch("/rules/{rule_id}", response_model=LoyaltyRuleResponse)
async def update_rule(rule_id: int, body: LoyaltyRuleUpdate, current_user=Depends(require_role("admin"))):
    """Met à jour une règle de bonus existante.

    Args:
        rule_id: Identifiant de la règle.
        body: Champs à mettre à jour.
        current_user: Utilisateur admin authentifié.

    Returns:
        Règle mise à jour.

    Raises:
        AppError: RULE_NOT_FOUND (404) si la règle est introuvable.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        rule = await session.get(LoyaltyRule, rule_id)
        if rule is None:
            raise AppError("RULE_NOT_FOUND", "Règle de fidélité introuvable", 404)
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(rule, field, value)
        config_service.validate_rule_state(rule)
        await session.commit()
        await session.refresh(rule)
        return LoyaltyRuleResponse.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, current_user=Depends(require_role("admin"))):
    """Supprime une règle de bonus.

    Args:
        rule_id: Identifiant de la règle.
        current_user: Utilisateur admin authentifié.

    Raises:
        AppError: RULE_NOT_FOUND (404) si la règle est introuvable.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        rule = await session.get(LoyaltyRule, rule_id)
        if rule is None:
            raise AppError("RULE_NOT_FOUND", "Règle de fidélité introuvable", 404)
        await session.delete(rule)
        await session.commit()


# ---------------------------------------------------------------------------
# Paliers de récompense (admin + public)
# ---------------------------------------------------------------------------


@router.get("/rewards", response_model=list[LoyaltyRewardEligibilityResponse])
async def list_rewards(current_user=Depends(get_current_user)):
    """Liste les récompenses disponibles.

    - Utilisateur authentifié : filtre par solde de points courant.
    - Admin : retourne toutes les récompenses actives.

    Args:
        current_user: Utilisateur authentifié (JWT).

    Returns:
        Liste des récompenses accessibles.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        account = await get_or_create_account(session, int(current_user["id"]))
        return await config_service.list_reward_catalog_with_eligibility(session, account.points)


@router.post("/rewards", response_model=LoyaltyRewardResponse, status_code=201)
async def create_reward(body: LoyaltyRewardCreate, current_user=Depends(require_role("admin"))):
    """Crée un nouveau palier de récompense.

    Args:
        body: Paramètres du palier.
        current_user: Utilisateur admin authentifié.

    Returns:
        Palier créé.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        reward = LoyaltyReward(**body.model_dump())
        config_service.validate_reward_state(reward)
        session.add(reward)
        await session.commit()
        await session.refresh(reward)
        return LoyaltyRewardResponse.model_validate(reward)


@router.patch("/rewards/{reward_id}", response_model=LoyaltyRewardResponse)
async def update_reward(reward_id: int, body: LoyaltyRewardUpdate, current_user=Depends(require_role("admin"))):
    """Met à jour un palier de récompense.

    Args:
        reward_id: Identifiant du palier.
        body: Champs à mettre à jour.
        current_user: Utilisateur admin authentifié.

    Returns:
        Palier mis à jour.

    Raises:
        AppError: REWARD_NOT_FOUND (404) si le palier est introuvable.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        reward = await session.get(LoyaltyReward, reward_id)
        if reward is None:
            raise AppError("REWARD_NOT_FOUND", "Palier de récompense introuvable", 404)
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(reward, field, value)
        config_service.validate_reward_state(reward)
        await session.commit()
        await session.refresh(reward)
        return LoyaltyRewardResponse.model_validate(reward)


@router.delete("/rewards/{reward_id}", status_code=204)
async def delete_reward(reward_id: int, current_user=Depends(require_role("admin"))):
    """Supprime un palier de récompense.

    Args:
        reward_id: Identifiant du palier.
        current_user: Utilisateur admin authentifié.

    Raises:
        AppError: REWARD_NOT_FOUND (404) si le palier est introuvable.
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        reward = await session.get(LoyaltyReward, reward_id)
        if reward is None:
            raise AppError("REWARD_NOT_FOUND", "Palier de récompense introuvable", 404)
        await session.delete(reward)
        await session.commit()


@router.post("/rewards/{reward_id}/redeem", response_model=RedeemResponse)
async def redeem_reward(reward_id: int, current_user=Depends(get_current_user)):
    """Échange un palier de récompense contre des points.

    Args:
        reward_id: Identifiant du palier à échanger.
        current_user: Utilisateur client authentifié.

    Returns:
        Résultat de l'échange (réduction ou product_id du produit offert).

    Raises:
        AppError: REWARD_NOT_FOUND (404) / INSUFFICIENT_POINTS (422).
    """
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await config_service.redeem_reward(session, int(current_user["id"]), reward_id)


@router.get("/stats", response_model=LoyaltyStatsResponse)
async def loyalty_stats(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    current_user=Depends(require_role("admin")),
):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await config_service.get_loyalty_stats(session, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Prévisualisation des points (client)
# ---------------------------------------------------------------------------


@router.get("/preview", response_model=LoyaltyPointsPreview)
async def preview_points(
    order_total: float = Query(..., gt=0, description="Montant total de la commande en euros"),
    category_ids: str = Query("", description="Liste de category_id séparés par des virgules"),
    current_user=Depends(get_current_user),
):
    """Prévisualise les points qu'une commande va générer.

    Utile pour afficher le gain de points avant validation panier.

    Args:
        order_total: Montant TTC de la commande.
        category_ids: Chaîne de category_id séparés par virgule (ex: "1,2,3").
        current_user: Utilisateur client authentifié.

    Returns:
        ``LoyaltyPointsPreview`` avec base, bonus et total.
    """
    parsed_ids: list[int] = []
    if category_ids:
        try:
            parsed_ids = [int(x.strip()) for x in category_ids.split(",") if x.strip()]
        except ValueError:
            raise AppError("INVALID_PARAMS", "category_ids doit être une liste d'entiers", 422, "category_ids")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await config_service.compute_points_for_order(
            session,
            order_total,
            parsed_ids,
            int(current_user["id"]),
        )
