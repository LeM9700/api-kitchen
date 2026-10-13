from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.catalog.schemas import ExtraOut, MediaImagePublicOut, ProductSummaryOut, VariantOut


class MenuCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    fixed_price: float = Field(ge=0, le=99_999_999.99)
    image_url: str | None = Field(default=None, max_length=512)
    display_order: int = Field(default=0, ge=0)
    is_active: bool = False
    available_from: datetime | None = None
    available_until: datetime | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Le nom du menu est obligatoire")
        return value

    @model_validator(mode="after")
    def validate_availability_window(self):
        if (
            self.available_from is not None
            and self.available_until is not None
            and self.available_until <= self.available_from
        ):
            raise ValueError("available_until doit etre posterieur a available_from")
        return self


class MenuUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    fixed_price: float | None = Field(default=None, ge=0, le=99_999_999.99)
    image_url: str | None = Field(default=None, max_length=512)
    display_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    available_from: datetime | None = None
    available_until: datetime | None = None
    change_reason: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Le nom du menu est obligatoire")
        return value


class MenuGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    min_choices: int = Field(default=1, ge=1)
    max_choices: int = Field(default=1, ge=1)
    display_order: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Le nom du groupe est obligatoire")
        return value

    @model_validator(mode="after")
    def validate_choice_range(self):
        if self.max_choices < self.min_choices:
            raise ValueError("max_choices doit etre superieur ou egal a min_choices")
        return self


class MenuGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    min_choices: int | None = Field(default=None, ge=1)
    max_choices: int | None = Field(default=None, ge=1)
    display_order: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Le nom du groupe est obligatoire")
        return value


class MenuGroupItemCreate(BaseModel):
    product_id: int = Field(gt=0)
    display_order: int = Field(default=0, ge=0)


class MenuGroupItemUpdate(BaseModel):
    display_order: int = Field(ge=0)


class MenuGroupItemAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    group_id: int
    product_id: int
    display_order: int
    product_name: str
    product_is_active: bool
    product_external_id: str | None = None


class MenuGroupAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    menu_id: int
    name: str
    min_choices: int
    max_choices: int
    display_order: int
    items: list[MenuGroupItemAdminOut] = Field(default_factory=list)


class MenuAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    fixed_price: float
    image_url: str | None = None
    display_order: int
    is_active: bool
    available_from: datetime | None = None
    available_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    groups: list[MenuGroupAdminOut] = Field(default_factory=list)
    is_publishable: bool
    validation_errors: list[str] = Field(default_factory=list)


class MenuAdminListOut(BaseModel):
    items: list[MenuAdminOut]
    total: int
    page: int
    page_size: int
    pages: int


class MenuProductPublicOut(ProductSummaryOut):
    menu_group_item_id: int
    display_order: int
    variants: list[VariantOut] = Field(default_factory=list)
    extras: list[ExtraOut] = Field(default_factory=list)
    gallery: list[MediaImagePublicOut] = Field(default_factory=list)


class MenuGroupPublicOut(BaseModel):
    id: int
    name: str
    min_choices: int
    max_choices: int
    display_order: int
    products: list[MenuProductPublicOut] = Field(default_factory=list)
    is_available: bool
    unavailable_reason: str | None = None


class MenuPublicOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    fixed_price: float
    image_url: str | None = None
    display_order: int
    available_from: datetime | None = None
    available_until: datetime | None = None
    groups: list[MenuGroupPublicOut] = Field(default_factory=list)
    is_available: bool
    unavailable_reasons: list[str] = Field(default_factory=list)
