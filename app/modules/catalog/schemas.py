from datetime import datetime
from math import ceil
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")
PreparationStation = Literal["kitchen", "counter", "none"]


class CatalogPaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    total_count: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[T], total: int, pagination) -> "CatalogPaginatedResponse[T]":
        pages = max(1, ceil(total / pagination.page_size))
        return cls(
            items=items,
            total=total,
            total_count=total,
            page=pagination.page,
            page_size=pagination.page_size,
            pages=pages,
        )


class CategoryCreate(BaseModel):
    name: str
    display_order: int = 0
    preparation_station: PreparationStation = "kitchen"


class CategoryUpdate(BaseModel):
    name: str | None = None
    display_order: int | None = None
    preparation_station: PreparationStation | None = None
    is_active: bool | None = None


class CategoryOut(CategoryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool


class CategorySummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_order: int
    preparation_station: PreparationStation = "kitchen"


class ProductCreate(BaseModel):
    category_id: int | None = None
    name: str
    description: str | None = None
    base_price: float
    image_url: str | None = None
    preparation_station: PreparationStation | None = None
    is_active: bool = True


class ProductUpdate(BaseModel):
    category_id: int | None = None
    name: str | None = None
    description: str | None = None
    base_price: float | None = None
    image_url: str | None = None
    preparation_station: PreparationStation | None = None
    is_active: bool | None = None
    price_change_reason: str | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int | None = None
    name: str
    description: str | None = None
    base_price: float
    image_url: str | None = None
    preparation_station: PreparationStation | None = None
    is_active: bool
    # Renseigne uniquement pour les tenants CONNECTED (HubCatalogProvider) --
    # toujours None/False pour STANDALONE (LocalCatalogProvider). Voir
    # docs/superpowers/specs/2026-08-11-hub-catalog-sync-design.md.
    tax_rate: float | None = None
    is_featured: bool = False


class ExtraCreate(BaseModel):
    name: str
    price: float
    is_active: bool = True


class ExtraUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    is_active: bool | None = None
    price_change_reason: str | None = None


class ExtraOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    price: float
    is_active: bool


class VariantCreate(BaseModel):
    name: str
    price_delta: float = 0.0
    is_available: bool = True


class VariantUpdate(BaseModel):
    name: str | None = None
    price_delta: float | None = None
    is_available: bool | None = None
    price_change_reason: str | None = None


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    name: str
    price_delta: float
    is_active: bool


class ProductExtraLink(BaseModel):
    """Reponse confirming an extra is linked/unlinked to a product."""

    product_id: int
    extra_id: int
    linked: bool


class MediaImagePublicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    url: str
    url_thumbnail: str
    url_medium: str
    format: str
    size_bytes: int
    width: int
    height: int
    is_primary: bool
    display_order: int
    alt_text: str | None = None
    created_at: datetime | None = None


class ProductAllergenPublicOut(BaseModel):
    allergen_id: int
    name: str
    slug: str
    level: Literal["present", "traces", "absent"]
    source: str
    is_regulatory: bool


class DietaryTagPublicOut(BaseModel):
    id: int
    name: str
    slug: str


class ProductAvailabilityOut(BaseModel):
    product_id: int
    available: bool
    limiting_ingredient: str | None = None
    reason: str | None = None
    is_overridden: bool = False


class ProductSummaryOut(ProductOut):
    category: CategorySummaryOut | None = None
    effective_preparation_station: PreparationStation = "kitchen"
    primary_image: MediaImagePublicOut | None = None
    allergens: list[ProductAllergenPublicOut] = Field(default_factory=list)
    dietary_tags: list[DietaryTagPublicOut] = Field(default_factory=list)
    availability: ProductAvailabilityOut | None = None
    regulatory_complete: bool = False


class ProductDetailOut(ProductSummaryOut):
    variants: list[VariantOut] = Field(default_factory=list)
    extras: list[ExtraOut] = Field(default_factory=list)
    gallery: list[MediaImagePublicOut] = Field(default_factory=list)
    recommendations: list[ProductSummaryOut] = Field(default_factory=list)


class ProductSuggestionOut(BaseModel):
    id: int
    name: str
    category_id: int | None = None
    primary_image_url: str | None = None


class ProductRecommendationCreate(BaseModel):
    recommended_product_id: int
    display_order: int = 0
    label: str | None = None


class ProductRecommendationUpdate(BaseModel):
    display_order: int | None = None
    label: str | None = None
    is_active: bool | None = None


class ProductRecommendationOut(BaseModel):
    id: int
    product_id: int
    recommended_product_id: int
    display_order: int
    label: str | None = None
    is_active: bool
    product: ProductSummaryOut | None = None


class CatalogCsvImportRequest(BaseModel):
    csv_text: str
    filename: str | None = None


class CatalogCsvImportError(BaseModel):
    row: int
    code: str
    detail: str


class CatalogCsvImportPreview(BaseModel):
    row: int
    action: str
    entity_type: str
    key: str


class CatalogCsvDryRunResponse(BaseModel):
    token: str
    valid: bool
    total_rows: int
    previews: list[CatalogCsvImportPreview] = Field(default_factory=list)
    errors: list[CatalogCsvImportError] = Field(default_factory=list)


class CatalogCsvConfirmResponse(BaseModel):
    token: str
    imported: bool
    total_rows: int
    created: int
    updated: int
    linked: int
    errors: list[CatalogCsvImportError] = Field(default_factory=list)


class PriceAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    old_price: float | None
    new_price: float
    changed_by_user_id: int | None
    source: str
    reason: str | None
    changed_at: datetime


class CatalogSelectionValidationRequest(BaseModel):
    product_id: int
    variant_id: int | None = None
    extra_ids: list[int] = Field(default_factory=list)


class CatalogSelectionValidationResponse(BaseModel):
    valid: bool
    product_id: int
    variant_id: int | None = None
    extra_ids: list[int] = Field(default_factory=list)
    regulatory_complete: bool
    allergens: list[ProductAllergenPublicOut] = Field(default_factory=list)


class ProductAvailabilityOverrideCreate(BaseModel):
    available: bool
    reason: str | None = Field(None, max_length=512)

    @model_validator(mode="after")
    def _validate_reason(self) -> "ProductAvailabilityOverrideCreate":
        if self.available is False and not (self.reason or "").strip():
            raise ValueError("reason est requis quand available=false")
        return self


class ProductAvailabilityOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    available: bool
    reason: str | None = None
    changed_by_user_id: int
    created_at: datetime


class NormalizedCatalogProduct(BaseModel):
    """Format pivot d'un produit tel que stocke dans catalog_snapshots.normalized.

    [HYPOTHESE NON CONFIRMEE] Champs derives d'une hypothese raisonnable sur le
    format de reponse du hub -- a confirmer avec le vrai fournisseur. Isole
    dans app/modules/catalog/normalize.py (seul point de mapping depuis le
    payload brut du hub).
    """

    external_id: str
    name: str
    description: str | None = None
    category: str | None = None
    price: float
    tax_rate: float | None = None
    image_url: str | None = None
    is_active: bool = True


class ProductOverrideCreate(BaseModel):
    """Donnees de presentation modifiables localement pour un produit synchronise
    depuis le hub. Aucun champ prix/TVA -- garde-fou fiscal au niveau schema,
    pas seulement applicatif (voir CatalogSnapshotUnavailableError et le design
    spec pour le contexte)."""

    model_config = ConfigDict(extra="forbid")

    image_url: str | None = None
    description: str | None = None
    is_featured: bool | None = None
    display_order: int | None = None


class ProductOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    image_url: str | None = None
    description: str | None = None
    is_featured: bool | None = None
    display_order: int | None = None
