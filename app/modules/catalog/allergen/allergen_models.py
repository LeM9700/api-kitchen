"""Modèles SQLAlchemy pour les allergènes réglementaires et les tags dietary.

Conforme au règlement UE n°1169/2011 (14 allergènes majeurs).
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AllergenChangeAudit(Base):
    """[🔒 SÉCURITÉ] Audit trail légal pour toute modification d'allergène.

    Chaque changement de niveau d'allergène (présent/traces/absent) est
    enregistré de manière immuable pour la traçabilité réglementaire.
    Règlement UE n°1169/2011 — obligation de traçabilité des informations
    sur les substances allergènes.

    Attributes:
        id: Clé primaire auto-incrémentée.
        product_id: Produit concerné.
        allergen_id: Allergène concerné.
        changed_by_user_id: Auteur du changement (0 = système automatique).
        changed_at: Horodatage UTC du changement.
        old_level: Niveau précédent (None si première déclaration).
        new_level: Nouveau niveau déclaré.
        old_source: Source précédente ('ingredient' ou 'manual').
        new_source: Nouvelle source.
        ip_address: Adresse IP de l'auteur (admin web).
        reason: Note optionnelle de justification.
    """

    __tablename__ = "allergen_change_audits"
    __table_args__ = (
        Index("ix_allergen_change_audits_product_changed", "product_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    allergen_id: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    old_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    new_level: Mapped[str] = mapped_column(String(10), nullable=False)
    old_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    new_source: Mapped[str] = mapped_column(String(10), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AllergenDefinition(Base):
    """Définition d'un allergène — réglementaire EU ou personnalisé par le tenant.

    Attributes:
        id: Clé primaire auto-incrémentée.
        name: Nom affiché (ex: "Gluten", "Lait").
        slug: Identifiant URL-safe unique (ex: "gluten", "milk").
        is_regulatory: True pour les 14 allergènes EU obligatoires.
        description: Description optionnelle.
        created_at: Horodatage de création (auto).
    """

    __tablename__ = "allergen_definitions"
    __table_args__ = (Index("ix_allergen_definitions_slug", "slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_regulatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class IngredientAllergen(Base):
    """Mapping ingrédient → allergène avec niveau de présence.

    Attributes:
        ingredient_id: FK vers ingredients.id (cascade delete).
        allergen_id: FK vers allergen_definitions.id (cascade delete).
        level: Niveau de présence : 'present', 'traces' ou 'absent'.
    """

    __tablename__ = "ingredient_allergens"
    __table_args__ = (
        CheckConstraint("level IN ('present', 'traces', 'absent')", name="ck_ingredient_allergen_level"),
    )

    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    allergen_id: Mapped[int] = mapped_column(
        ForeignKey("allergen_definitions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)


class ProductAllergen(Base):
    """Allergènes déclarés sur un produit — calculés depuis les ingrédients ou saisis manuellement.

    Attributes:
        product_id: FK vers products.id (cascade delete).
        allergen_id: FK vers allergen_definitions.id (cascade delete).
        level: Niveau : 'present', 'traces' ou 'absent'.
        source: 'ingredient' = calculé automatiquement, 'manual' = déclaration manuelle prioritaire.
    """

    __tablename__ = "product_allergens"
    __table_args__ = (
        CheckConstraint("level IN ('present', 'traces', 'absent')", name="ck_product_allergen_level"),
        CheckConstraint("source IN ('ingredient', 'manual')", name="ck_product_allergen_source"),
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        primary_key=True,
    )
    allergen_id: Mapped[int] = mapped_column(
        ForeignKey("allergen_definitions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="ingredient")


class DietaryTag(Base):
    """Tag régime alimentaire applicable à un produit.

    Attributes:
        id: Clé primaire auto-incrémentée.
        name: Libellé affiché (ex: "Végétarien", "Halal").
        slug: Identifiant URL-safe unique (ex: "vegetarien", "halal").
        created_at: Horodatage de création (auto).
    """

    __tablename__ = "dietary_tags"
    __table_args__ = (Index("ix_dietary_tags_slug", "slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class ProductDietaryTag(Base):
    """Relation M2M produit ↔ tag dietary.

    Attributes:
        product_id: FK vers products.id (cascade delete).
        dietary_tag_id: FK vers dietary_tags.id (cascade delete).
    """

    __tablename__ = "product_dietary_tags"

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dietary_tag_id: Mapped[int] = mapped_column(
        ForeignKey("dietary_tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
