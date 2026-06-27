"""Schémas Pydantic pour les allergènes réglementaires et les tags dietary."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class AllergenDefinitionCreate(BaseModel):
    """Payload de création d'un allergène personnalisé (non réglementaire).

    Attributes:
        name: Nom affiché de l'allergène.
        slug: Identifiant URL-safe unique.
        description: Description optionnelle.
    """

    name: str
    slug: str
    description: str | None = None


class AllergenDefinitionResponse(AllergenDefinitionCreate):
    """Réponse complète d'une définition d'allergène.

    Attributes:
        id: Clé primaire.
        is_regulatory: True si allergène EU obligatoire.
        created_at: Horodatage de création.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_regulatory: bool
    created_at: datetime


class IngredientAllergenSet(BaseModel):
    """Payload pour associer un allergène à un ingrédient.

    Attributes:
        allergen_id: ID de l'allergène.
        level: Niveau de présence dans l'ingrédient.
    """

    allergen_id: int
    level: Literal["present", "traces", "absent"]


class ProductAllergenResponse(BaseModel):
    """Détail d'un allergène déclaré sur un produit.

    Attributes:
        allergen_id: ID de l'allergène.
        allergen_name: Nom affiché.
        allergen_slug: Slug de l'allergène.
        level: Niveau de présence effectif.
        source: Origine de la déclaration ('ingredient' ou 'manual').
        is_regulatory: True si allergène EU obligatoire.
    """

    model_config = ConfigDict(from_attributes=True)

    allergen_id: int
    allergen_name: str
    allergen_slug: str
    level: Literal["present", "traces", "absent"]
    source: Literal["ingredient", "manual"]
    is_regulatory: bool


class ProductAllergenPatch(BaseModel):
    """Payload pour une déclaration manuelle d'allergène sur un produit.

    Attributes:
        level: Niveau de présence déclaré manuellement.
        reason: Justification optionnelle du changement (traçabilité réglementaire).
    """

    level: Literal["present", "traces", "absent"]
    reason: str | None = None


class AllergenChangeAuditResponse(BaseModel):
    """Entrée d'audit d'un changement d'allergène sur un produit.

    [🔒 SÉCURITÉ] Données de traçabilité réglementaire — accès admin uniquement.

    Attributes:
        id: Clé primaire.
        product_id: Produit concerné.
        allergen_id: Allergène concerné.
        changed_by_user_id: Auteur (0 = recalcul automatique système).
        changed_at: Horodatage UTC.
        old_level: Niveau précédent (None = première déclaration).
        new_level: Nouveau niveau.
        old_source: Source précédente.
        new_source: Nouvelle source.
        ip_address: IP de l'auteur.
        reason: Justification.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    allergen_id: int
    changed_by_user_id: int
    changed_at: datetime
    old_level: str | None
    new_level: str
    old_source: str | None
    new_source: str
    ip_address: str | None
    reason: str | None


class DietaryTagResponse(BaseModel):
    """Réponse d'un tag dietary.

    Attributes:
        id: Clé primaire.
        name: Libellé affiché.
        slug: Identifiant URL-safe.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class ProductDietaryTagsSet(BaseModel):
    """Payload pour remplacer les tags dietary d'un produit.

    Attributes:
        tag_ids: Liste des IDs de tags à appliquer (remplace l'existant).
    """

    tag_ids: list[int]


class ProductAllergenSummary(BaseModel):
    """Résumé complet des allergènes et tags d'un produit.

    Attributes:
        allergens: Liste des allergènes déclarés (calcul auto + manuels mergés).
        dietary_tags: Tags dietary actifs sur le produit.
        regulatory_complete: True si les 14 allergènes EU ont tous un niveau déclaré.
    """

    allergens: list[ProductAllergenResponse]
    dietary_tags: list[DietaryTagResponse]
    regulatory_complete: bool
