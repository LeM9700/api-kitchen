"""Ajout des tables allergènes réglementaires et dietary tags + index GIN full-text

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-22

Tables créées (par schema tenant) :
- allergen_definitions  : 14 allergènes EU + extensions libres
- ingredient_allergens  : mapping ingrédient → allergène avec niveau
- product_allergens     : allergènes déclarés sur un produit (auto ou manuel)
- dietary_tags          : tags régime alimentaire
- product_dietary_tags  : M2M produit ↔ tag dietary

Index :
- GIN sur products(to_tsvector('french', name || description)) pour le FTS
"""

import sqlalchemy as sa
from alembic import op

revision = "0009b"
down_revision = "0009a"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Données initiales
# ---------------------------------------------------------------------------

_REGULATORY_ALLERGENS = [
    ("Gluten", "gluten", "Céréales contenant du gluten", True),
    ("Crustacés", "crustaces", "Crustacés et produits à base de crustacés", True),
    ("Œufs", "oeufs", "Œufs et produits à base d'œufs", True),
    ("Poisson", "poisson", "Poissons et produits à base de poissons", True),
    ("Cacahuètes", "cacahuetes", "Arachides et produits à base d'arachides", True),
    ("Soja", "soja", "Soja et produits à base de soja", True),
    ("Lait", "lait", "Lait et produits laitiers (lactose inclus)", True),
    ("Fruits à coque", "fruits-a-coque", "Noix, noisettes, amandes, etc.", True),
    ("Céleri", "celeri", "Céleri et produits à base de céleri", True),
    ("Moutarde", "moutarde", "Moutarde et produits à base de moutarde", True),
    ("Sésame", "sesame", "Graines de sésame et produits à base de graines de sésame", True),
    ("Sulfites", "sulfites", "Anhydride sulfureux et sulfites > 10 mg/kg ou mg/litre", True),
    ("Lupin", "lupin", "Lupin et produits à base de lupin", True),
    ("Mollusques", "mollusques", "Mollusques et produits à base de mollusques", True),
]

_DEFAULT_DIETARY_TAGS = [
    ("Végétarien", "vegetarien"),
    ("Vegan", "vegan"),
    ("Sans gluten", "sans-gluten"),
    ("Halal", "halal"),
    ("Casher", "casher"),
    ("Sans lactose", "sans-lactose"),
    ("Sans noix", "sans-noix"),
    ("Bio", "bio"),
]


def _get_tenant_slugs(bind) -> list[str]:
    """Récupère tous les slugs de tenants existants depuis le schema public.

    Args:
        bind: Connexion SQLAlchemy active (``op.get_bind()``).

    Returns:
        Liste des slugs de tenants.
    """
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Crée les tables allergènes et dietary tags dans chaque schema tenant.

    [⚠️ PROD] Itère sur tous les tenants existants. Les nouveaux tenants créés après
    cette migration doivent inclure ces tables dans leur script de provisioning.

    [⚡ PERF] L'index GIN sur products(tsvector) accélère drastiquement la recherche
    full-text. Prévoir ~50-200ms de lock court sur la table products selon le volume.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        # --- allergen_definitions ---
        op.create_table(
            "allergen_definitions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("is_regulatory", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("slug", name=f"uq_allergen_definitions_slug_{slug}"),
            schema=schema,
        )
        op.create_index(
            f"ix_allergen_definitions_slug_{slug}",
            "allergen_definitions",
            ["slug"],
            schema=schema,
        )

        # --- ingredient_allergens ---
        op.create_table(
            "ingredient_allergens",
            sa.Column("ingredient_id", sa.Integer(), sa.ForeignKey(f"{schema}.ingredients.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("allergen_id", sa.Integer(), sa.ForeignKey(f"{schema}.allergen_definitions.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("level", sa.String(10), nullable=False),
            sa.CheckConstraint("level IN ('present', 'traces', 'absent')", name=f"ck_ingredient_allergen_level_{slug}"),
            schema=schema,
        )

        # --- product_allergens ---
        op.create_table(
            "product_allergens",
            sa.Column("product_id", sa.Integer(), sa.ForeignKey(f"{schema}.products.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("allergen_id", sa.Integer(), sa.ForeignKey(f"{schema}.allergen_definitions.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("level", sa.String(10), nullable=False),
            sa.Column("source", sa.String(10), nullable=False, server_default="ingredient"),
            sa.CheckConstraint("level IN ('present', 'traces', 'absent')", name=f"ck_product_allergen_level_{slug}"),
            sa.CheckConstraint("source IN ('ingredient', 'manual')", name=f"ck_product_allergen_source_{slug}"),
            schema=schema,
        )

        # --- dietary_tags ---
        op.create_table(
            "dietary_tags",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("slug", name=f"uq_dietary_tags_slug_{slug}"),
            schema=schema,
        )
        op.create_index(
            f"ix_dietary_tags_slug_{slug}",
            "dietary_tags",
            ["slug"],
            schema=schema,
        )

        # --- product_dietary_tags ---
        op.create_table(
            "product_dietary_tags",
            sa.Column("product_id", sa.Integer(), sa.ForeignKey(f"{schema}.products.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("dietary_tag_id", sa.Integer(), sa.ForeignKey(f"{schema}.dietary_tags.id", ondelete="CASCADE"), primary_key=True),
            schema=schema,
        )

        # --- Index GIN full-text sur products ---
        # [⚡ PERF] CONCURRENTLY non disponible en transaction — acceptable en migration offline.
        bind.execute(sa.text(
            f"CREATE INDEX idx_products_fts_{slug} "
            f"ON \"{schema}\".products "
            f"USING GIN(to_tsvector('french', name || ' ' || COALESCE(description, '')))"
        ))

        # --- Seed données initiales ---
        for name, slug_val, description, is_regulatory in _REGULATORY_ALLERGENS:
            bind.execute(sa.text(
                f"INSERT INTO \"{schema}\".allergen_definitions (name, slug, description, is_regulatory) "
                f"VALUES (:name, :slug, :description, :is_regulatory) "
                f"ON CONFLICT (slug) DO NOTHING"
            ), {"name": name, "slug": slug_val, "description": description, "is_regulatory": is_regulatory})

        for name, slug_val in _DEFAULT_DIETARY_TAGS:
            bind.execute(sa.text(
                f"INSERT INTO \"{schema}\".dietary_tags (name, slug) "
                f"VALUES (:name, :slug) "
                f"ON CONFLICT (slug) DO NOTHING"
            ), {"name": name, "slug": slug_val})


def downgrade() -> None:
    """Supprime les tables allergènes et dietary tags de chaque schema tenant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        bind.execute(sa.text(f"DROP INDEX IF EXISTS \"{schema}\".idx_products_fts_{slug}"))

        op.drop_table("product_dietary_tags", schema=schema)
        op.drop_index(f"ix_dietary_tags_slug_{slug}", table_name="dietary_tags", schema=schema)
        op.drop_table("dietary_tags", schema=schema)
        op.drop_table("product_allergens", schema=schema)
        op.drop_table("ingredient_allergens", schema=schema)
        op.drop_index(f"ix_allergen_definitions_slug_{slug}", table_name="allergen_definitions", schema=schema)
        op.drop_table("allergen_definitions", schema=schema)
