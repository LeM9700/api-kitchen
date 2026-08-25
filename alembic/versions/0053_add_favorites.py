"""add favorites table to tenant schemas

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    """Récupère tous les slugs de tenants existants depuis le schema public.

    Args:
        bind: Connexion SQLAlchemy active (``op.get_bind()``).

    Returns:
        Liste des slugs de tenants.
    """
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def _table_exists(bind, schema: str, table: str) -> bool:
    return bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    ).scalar() is not None


def upgrade() -> None:
    """Crée la table ``favorites`` dans chaque schema tenant existant.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration reçoivent la table via
    ``Base.metadata.create_all`` au provisioning (voir
    ``app/modules/auth/service.py::_provision_tenant_schema`` et
    ``app/core/database/tenant_models.py``).

    Idempotent (contrairement à 0008, son analogue pour ``device_tokens``) :
    un tenant déjà provisionné avec ``favorites`` via ``create_all`` (ex.
    tenants de test recréés par ``tests/conftest.py::bootstrap_default_tenant``
    avant que cette migration ne soit rejouée) est sauté plutôt que de faire
    échouer toute la migration sur un ``DuplicateTableError``.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if _table_exists(bind, schema, "favorites"):
            continue
        op.create_table(
            "favorites",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "user_id", "product_id", name=f"uq_favorite_user_product_{slug}"
            ),
            schema=schema,
        )
        op.create_index(
            f"ix_favorites_user_id_{slug}",
            "favorites",
            ["user_id"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table ``favorites`` de chaque schema tenant existant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(f"ix_favorites_user_id_{slug}", table_name="favorites", schema=schema)
        op.drop_table("favorites", schema=schema)
