"""Service de gestion des allergènes réglementaires et tags dietary.

Règlement UE n°1169/2011 — 14 allergènes majeurs obligatoires.
"""

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.modules.catalog.allergen.allergen_models import (
    AllergenChangeAudit,
    AllergenDefinition,
    DietaryTag,
    IngredientAllergen,
    ProductAllergen,
    ProductDietaryTag,
)

logger = logging.getLogger(__name__)
from app.modules.catalog.allergen.allergen_schemas import (
    AllergenDefinitionCreate,
    DietaryTagResponse,
    IngredientAllergenSet,
    ProductAllergenResponse,
    ProductAllergenSummary,
)

# ---------------------------------------------------------------------------
# Données de référence
# ---------------------------------------------------------------------------

_REGULATORY_ALLERGENS: list[dict] = [
    {"name": "Gluten", "slug": "gluten", "description": "Céréales contenant du gluten"},
    {"name": "Crustacés", "slug": "crustaces", "description": "Crustacés et produits à base de crustacés"},
    {"name": "Œufs", "slug": "oeufs", "description": "Œufs et produits à base d'œufs"},
    {"name": "Poisson", "slug": "poisson", "description": "Poissons et produits à base de poissons"},
    {"name": "Cacahuètes", "slug": "cacahuetes", "description": "Arachides et produits à base d'arachides"},
    {"name": "Soja", "slug": "soja", "description": "Soja et produits à base de soja"},
    {"name": "Lait", "slug": "lait", "description": "Lait et produits laitiers (lactose inclus)"},
    {"name": "Fruits à coque", "slug": "fruits-a-coque", "description": "Noix, noisettes, amandes, etc."},
    {"name": "Céleri", "slug": "celeri", "description": "Céleri et produits à base de céleri"},
    {"name": "Moutarde", "slug": "moutarde", "description": "Moutarde et produits à base de moutarde"},
    {"name": "Sésame", "slug": "sesame", "description": "Graines de sésame et produits à base de graines de sésame"},
    {"name": "Sulfites", "slug": "sulfites", "description": "Anhydride sulfureux et sulfites > 10 mg/kg ou mg/litre"},
    {"name": "Lupin", "slug": "lupin", "description": "Lupin et produits à base de lupin"},
    {"name": "Mollusques", "slug": "mollusques", "description": "Mollusques et produits à base de mollusques"},
]

_DEFAULT_DIETARY_TAGS: list[dict] = [
    {"name": "Végétarien", "slug": "vegetarien"},
    {"name": "Vegan", "slug": "vegan"},
    {"name": "Sans gluten", "slug": "sans-gluten"},
    {"name": "Halal", "slug": "halal"},
    {"name": "Casher", "slug": "casher"},
    {"name": "Sans lactose", "slug": "sans-lactose"},
    {"name": "Sans noix", "slug": "sans-noix"},
    {"name": "Bio", "slug": "bio"},
]

# Ordre de priorité des niveaux (le plus haut gagne lors du merge ingrédients)
_LEVEL_PRIORITY: dict[str, int] = {"present": 2, "traces": 1, "absent": 0}


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed_regulatory_allergens(session: AsyncSession) -> None:
    """Insère les 14 allergènes EU et les tags dietary de base si absents.

    Idempotent : utilise INSERT … ON CONFLICT DO NOTHING.
    À appeler lors du provisioning d'un nouveau tenant ou en migration.

    Args:
        session: Session tenant-scoped (search_path déjà positionné).
    """
    for data in _REGULATORY_ALLERGENS:
        stmt = (
            pg_insert(AllergenDefinition)
            .values(name=data["name"], slug=data["slug"], description=data["description"], is_regulatory=True)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        await session.execute(stmt)

    for data in _DEFAULT_DIETARY_TAGS:
        stmt = (
            pg_insert(DietaryTag)
            .values(name=data["name"], slug=data["slug"])
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        await session.execute(stmt)

    await session.commit()


# ---------------------------------------------------------------------------
# Ingrédients → allergènes
# ---------------------------------------------------------------------------


async def set_ingredient_allergens(
    session: AsyncSession,
    ingredient_id: int,
    allergens: list[IngredientAllergenSet],
) -> list[IngredientAllergen]:
    """Remplace tous les allergènes d'un ingrédient et propage aux produits associés.

    Args:
        session: Session tenant-scoped.
        ingredient_id: Clé primaire de l'ingrédient.
        allergens: Liste des associations (allergen_id, level) à appliquer.

    Returns:
        Liste des IngredientAllergen persistés.
    """
    # 1. Supprimer les mappings existants
    await session.execute(
        delete(IngredientAllergen).where(IngredientAllergen.ingredient_id == ingredient_id)
    )

    # 2. Insérer les nouveaux mappings
    new_mappings = [
        IngredientAllergen(ingredient_id=ingredient_id, allergen_id=item.allergen_id, level=item.level)
        for item in allergens
    ]
    session.add_all(new_mappings)
    await session.flush()

    # 3. Propager à tous les produits contenant cet ingrédient
    from app.modules.stock.models import ProductIngredient

    result = await session.execute(
        select(ProductIngredient.product_id).where(ProductIngredient.ingredient_id == ingredient_id)
    )
    product_ids = list(result.scalars())

    for product_id in product_ids:
        await _recompute_product_allergens_inner(session, product_id)

    await session.commit()
    return new_mappings


async def get_ingredient_allergens(
    session: AsyncSession,
    ingredient_id: int,
) -> list[IngredientAllergen]:
    """Retourne les allergènes associés à un ingrédient.

    Args:
        session: Session tenant-scoped.
        ingredient_id: Clé primaire de l'ingrédient.

    Returns:
        Liste des IngredientAllergen.
    """
    result = await session.execute(
        select(IngredientAllergen).where(IngredientAllergen.ingredient_id == ingredient_id)
    )
    return list(result.scalars())


# ---------------------------------------------------------------------------
# Recalcul allergènes produit depuis les ingrédients
# ---------------------------------------------------------------------------


async def _recompute_product_allergens_inner(session: AsyncSession, product_id: int) -> None:
    """Logique interne de recalcul (sans commit — le caller commit).

    Calcule les allergènes depuis les ingrédients du produit et upsert dans
    product_allergens avec source='ingredient'. Ne touche pas aux entrées 'manual'.

    Args:
        session: Session tenant-scoped.
        product_id: Clé primaire du produit.
    """
    from app.modules.stock.models import ProductIngredient

    # Récupère tous les (allergen_id, level) depuis les ingrédients du produit
    result = await session.execute(
        select(IngredientAllergen.allergen_id, IngredientAllergen.level)
        .join(ProductIngredient, ProductIngredient.ingredient_id == IngredientAllergen.ingredient_id)
        .where(ProductIngredient.product_id == product_id)
    )
    rows = result.all()

    # Agrège : pour chaque allergen_id, on prend le niveau MAX (present > traces > absent)
    best_levels: dict[int, str] = {}
    for allergen_id, level in rows:
        current_priority = _LEVEL_PRIORITY.get(best_levels.get(allergen_id, "absent"), 0)
        if _LEVEL_PRIORITY[level] > current_priority:
            best_levels[allergen_id] = level

    # Supprime uniquement les entrées 'ingredient' existantes
    await session.execute(
        delete(ProductAllergen).where(
            ProductAllergen.product_id == product_id,
            ProductAllergen.source == "ingredient",
        )
    )

    # Insère les nouvelles entrées calculées
    for allergen_id, level in best_levels.items():
        session.add(ProductAllergen(
            product_id=product_id,
            allergen_id=allergen_id,
            level=level,
            source="ingredient",
        ))


async def recompute_product_allergens(session: AsyncSession, product_id: int) -> None:
    """Recalcule et persiste les allergènes d'un produit depuis ses ingrédients.

    [⚠️ PROD] N'écrase pas les déclarations manuelles (source='manual').
    [🔒 SÉCURITÉ] Génère un AllergenChangeAudit pour chaque allergène modifié.

    Args:
        session: Session tenant-scoped.
        product_id: Clé primaire du produit.
    """
    # Capture l'état avant recalcul pour l'audit
    before_result = await session.execute(
        select(ProductAllergen).where(
            ProductAllergen.product_id == product_id,
            ProductAllergen.source == "ingredient",
        )
    )
    before_map: dict[int, ProductAllergen] = {
        row.allergen_id: row for row in before_result.scalars()
    }

    await _recompute_product_allergens_inner(session, product_id)
    await session.flush()

    # Capture l'état après recalcul
    after_result = await session.execute(
        select(ProductAllergen).where(
            ProductAllergen.product_id == product_id,
            ProductAllergen.source == "ingredient",
        )
    )
    after_map: dict[int, ProductAllergen] = {
        row.allergen_id: row for row in after_result.scalars()
    }

    # Génère les entrées d'audit pour chaque allergène créé ou modifié
    all_allergen_ids = set(before_map) | set(after_map)
    for allergen_id in all_allergen_ids:
        old = before_map.get(allergen_id)
        new = after_map.get(allergen_id)
        old_level = old.level if old else None
        new_level = new.level if new else "absent"
        if old_level != new_level:
            session.add(AllergenChangeAudit(
                product_id=product_id,
                allergen_id=allergen_id,
                changed_by_user_id=0,
                old_level=old_level,
                new_level=new_level,
                old_source="ingredient",
                new_source="ingredient",
            ))

    await session.commit()


# ---------------------------------------------------------------------------
# Déclarations manuelles sur un produit
# ---------------------------------------------------------------------------


async def set_product_allergen_manual(
    session: AsyncSession,
    product_id: int,
    allergen_id: int,
    level: str,
    user_id: int = 0,
    ip_address: str | None = None,
    reason: str | None = None,
) -> ProductAllergen:
    """Upsert une déclaration manuelle d'allergène sur un produit.

    La déclaration manuelle a la priorité absolue sur le calcul automatique.
    [🔒 SÉCURITÉ] Génère un AllergenChangeAudit immuable à chaque appel.
    [🔒 SÉCURITÉ] Notifie les clients ayant commandé ce produit dans les 30 derniers
    jours si le niveau change et devient non-absent.

    Args:
        session: Session tenant-scoped.
        product_id: Clé primaire du produit.
        allergen_id: Clé primaire de l'allergène.
        level: Niveau déclaré : 'present', 'traces' ou 'absent'.
        user_id: ID de l'utilisateur auteur du changement.
        ip_address: IP de la requête entrante (traçabilité).
        reason: Justification optionnelle.

    Returns:
        Instance ProductAllergen persistée.
    """
    # Capture l'état précédent avant l'upsert
    existing = await session.get(ProductAllergen, (product_id, allergen_id))
    old_level: str | None = existing.level if existing else None
    old_source: str | None = existing.source if existing else None

    if existing is not None:
        existing.level = level
        existing.source = "manual"
        await session.flush()
        await session.refresh(existing)
        entry = existing
    else:
        entry = ProductAllergen(
            product_id=product_id,
            allergen_id=allergen_id,
            level=level,
            source="manual",
        )
        session.add(entry)
        await session.flush()
        await session.refresh(entry)

    # [🔒 SÉCURITÉ] Audit immuable — toujours enregistré, même si level identique
    session.add(AllergenChangeAudit(
        product_id=product_id,
        allergen_id=allergen_id,
        changed_by_user_id=user_id,
        old_level=old_level,
        new_level=level,
        old_source=old_source,
        new_source="manual",
        ip_address=ip_address,
        reason=reason,
    ))

    await session.commit()
    await session.refresh(entry)

    # [🔒 SÉCURITÉ] Notification clients si le niveau d'allergène change et devient actif
    if old_level != level and level != "absent":
        try:
            from datetime import datetime, timedelta, timezone

            from sqlalchemy import distinct

            from app.modules.orders.models import Order, OrderItem
            from app.modules.notifications.notification_service import notify_user

            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            user_ids_result = await session.execute(
                select(distinct(Order.user_id))
                .join(OrderItem, OrderItem.order_id == Order.id)
                .where(
                    OrderItem.product_id == product_id,
                    Order.user_id.is_not(None),
                    Order.created_at > cutoff,
                )
            )
            affected_user_ids = list(user_ids_result.scalars())

            for uid in affected_user_ids:
                await notify_user(
                    session=session,
                    tenant_slug="",   # le caller peut passer tenant_slug via kwargs si besoin
                    user_id=uid,
                    event="allergen_update",
                    title="Information allergènes mise à jour",
                    body=(
                        "Les informations allergènes d'un produit que vous avez commandé "
                        "ont été mises à jour. Consultez la fiche produit avant votre "
                        "prochaine commande."
                    ),
                    data={"product_id": product_id, "allergen_id": allergen_id},
                )
        except Exception as exc:
            logger.error(
                "set_product_allergen_manual: notification client échouée "
                "product_id=%s allergen_id=%s: %s",
                product_id,
                allergen_id,
                exc,
            )

    return entry


# ---------------------------------------------------------------------------
# Résumé allergènes produit
# ---------------------------------------------------------------------------


async def get_product_allergen_summary(
    session: AsyncSession,
    product_id: int,
) -> ProductAllergenSummary:
    """Construit le résumé allergènes d'un produit (merge ingredient + manual).

    Règle de merge : pour chaque allergène déclaré, source='manual' prime sur
    source='ingredient'. regulatory_complete = True si les 14 allergènes EU
    ont tous une entrée (quelle que soit la valeur de level).

    Args:
        session: Session tenant-scoped.
        product_id: Clé primaire du produit.

    Returns:
        ProductAllergenSummary avec allergènes mergés et tags dietary.
    """
    # Récupère toutes les entrées du produit
    result = await session.execute(
        select(ProductAllergen, AllergenDefinition)
        .join(AllergenDefinition, AllergenDefinition.id == ProductAllergen.allergen_id)
        .where(ProductAllergen.product_id == product_id)
    )
    rows = result.all()

    # Merge : manual prime sur ingredient pour un même allergen_id
    merged: dict[int, ProductAllergenResponse] = {}
    for pa, ad in rows:
        existing = merged.get(pa.allergen_id)
        if existing is None or pa.source == "manual":
            merged[pa.allergen_id] = ProductAllergenResponse(
                allergen_id=pa.allergen_id,
                allergen_name=ad.name,
                allergen_slug=ad.slug,
                level=pa.level,
                source=pa.source,
                is_regulatory=ad.is_regulatory,
            )

    # Calcul regulatory_complete
    regulatory_ids_result = await session.execute(
        select(AllergenDefinition.id).where(AllergenDefinition.is_regulatory.is_(True))
    )
    regulatory_ids = set(regulatory_ids_result.scalars())
    declared_ids = set(merged.keys())
    regulatory_complete = regulatory_ids.issubset(declared_ids)

    # Tags dietary
    dietary_tags = await get_product_dietary_tags(session, product_id)
    dietary_responses = [DietaryTagResponse.model_validate(tag) for tag in dietary_tags]

    return ProductAllergenSummary(
        allergens=list(merged.values()),
        dietary_tags=dietary_responses,
        regulatory_complete=regulatory_complete,
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def get_product_allergen_audit(
    session: AsyncSession,
    product_id: int,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AllergenChangeAudit], int]:
    """Retourne l'historique pagine des changements d'allergenes d'un produit.

    Acces admin uniquement.

    Args:
        session: Session tenant-scoped.
        product_id: Cle primaire du produit.
        limit: Nombre max d'entrees.
        offset: Decalage pour la pagination.

    Returns:
        Tuple (liste d'AllergenChangeAudit, total).
    """
    base_where = AllergenChangeAudit.product_id == product_id

    total_result = await session.scalar(
        select(func.count(AllergenChangeAudit.id)).where(base_where)
    )
    total = total_result or 0

    rows_result = await session.execute(
        select(AllergenChangeAudit)
        .where(base_where)
        .order_by(AllergenChangeAudit.changed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows_result.scalars()), total


# ---------------------------------------------------------------------------
# Validation avant publication
# ---------------------------------------------------------------------------


async def validate_product_for_publication(session: AsyncSession, product_id: int) -> None:
    """Verifie que les 14 allergenes reglementaires sont tous declares.

    Args:
        session: Session tenant-scoped.
        product_id: Cle primaire du produit.

    Raises:
        HTTPException: 422 si des allergenes reglementaires manquent.
    """
    result = await session.execute(
        select(AllergenDefinition.name)
        .where(AllergenDefinition.is_regulatory.is_(True))
        .where(
            AllergenDefinition.id.not_in(
                select(ProductAllergen.allergen_id).where(ProductAllergen.product_id == product_id)
            )
        )
        .order_by(AllergenDefinition.name)
    )
    missing = list(result.scalars())
    if missing:
        names_str = ", ".join(missing)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Ce produit ne peut pas etre publie : les allergenes reglementaires "
                f"suivants ne sont pas declares : {names_str}"
            ),
        )


# ---------------------------------------------------------------------------
# Dietary tags
# ---------------------------------------------------------------------------


async def set_product_dietary_tags(
    session: AsyncSession,
    product_id: int,
    tag_ids: list[int],
) -> list[DietaryTag]:
    """Remplace les dietary tags d'un produit.

    Args:
        session: Session tenant-scoped.
        product_id: Cle primaire du produit.
        tag_ids: Liste des IDs de tags a appliquer (vide = tout supprimer).

    Returns:
        Liste des DietaryTag actifs apres mise a jour.
    """
    await session.execute(
        delete(ProductDietaryTag).where(ProductDietaryTag.product_id == product_id)
    )
    for tag_id in tag_ids:
        session.add(ProductDietaryTag(product_id=product_id, dietary_tag_id=tag_id))
    await session.commit()
    return await get_product_dietary_tags(session, product_id)


async def get_product_dietary_tags(
    session: AsyncSession,
    product_id: int,
) -> list[DietaryTag]:
    """Retourne les dietary tags d'un produit.

    Args:
        session: Session tenant-scoped.
        product_id: Cle primaire du produit.

    Returns:
        Liste des DietaryTag ordonnee par nom.
    """
    result = await session.execute(
        select(DietaryTag)
        .join(ProductDietaryTag, ProductDietaryTag.dietary_tag_id == DietaryTag.id)
        .where(ProductDietaryTag.product_id == product_id)
        .order_by(DietaryTag.name)
    )
    return list(result.scalars())


async def list_dietary_tags(session: AsyncSession) -> list[DietaryTag]:
    """Retourne tous les dietary tags disponibles dans le tenant.

    Args:
        session: Session tenant-scoped.

    Returns:
        Liste des DietaryTag ordonnee par nom.
    """
    result = await session.execute(select(DietaryTag).order_by(DietaryTag.name))
    return list(result.scalars())


async def list_allergen_definitions(session: AsyncSession) -> list[AllergenDefinition]:
    """Retourne toutes les definitions d'allergenes du tenant.

    Args:
        session: Session tenant-scoped.

    Returns:
        Liste ordonnee : reglementaires d'abord, puis personnalises.
    """
    result = await session.execute(
        select(AllergenDefinition).order_by(
            AllergenDefinition.is_regulatory.desc(), AllergenDefinition.name
        )
    )
    return list(result.scalars())


async def create_custom_allergen(
    session: AsyncSession,
    data: AllergenDefinitionCreate,
) -> AllergenDefinition:
    """Cree un allergene personnalise (non reglementaire).

    is_regulatory est force a False.

    Args:
        session: Session tenant-scoped.
        data: Payload de creation.

    Returns:
        Instance AllergenDefinition persistee.

    Raises:
        HTTPException: 409 si le slug existe deja.
        HTTPException: 422 si la limite de 50 allergenes custom est atteinte.
    """
    custom_count = await session.scalar(
        select(func.count(AllergenDefinition.id)).where(
            AllergenDefinition.is_regulatory == False  # noqa: E712
        )
    )
    if custom_count >= 50:
        raise HTTPException(
            status_code=422,
            detail="Limite de 50 allergenes personnalises atteinte pour ce tenant.",
        )

    existing = await session.execute(
        select(AllergenDefinition).where(AllergenDefinition.slug == data.slug)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Un allergene avec le slug '{data.slug}' existe deja.",
        )

    allergen = AllergenDefinition(
        name=data.name,
        slug=data.slug,
        description=data.description,
        is_regulatory=False,
    )
    session.add(allergen)
    await session.commit()
    await session.refresh(allergen)
    return allergen
