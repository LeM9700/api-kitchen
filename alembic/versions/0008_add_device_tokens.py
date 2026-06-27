"""add device_tokens table to tenant schemas

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
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


def upgrade() -> None:
    """Crée la table ``device_tokens`` dans chaque schema tenant existant.

    [⚠️ PROD] Itère sur tous les tenants existants au moment de la migration.
    Les nouveaux tenants créés après cette migration doivent inclure la table
    dans leur script de provisioning.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.create_table(
            "device_tokens",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(10), nullable=False),
            sa.Column("token", sa.String(512), nullable=False),
            sa.Column("device_name", sa.String(128), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "token", name=f"uq_device_token_user_{slug}"),
            schema=schema,
        )
        op.create_index(
            f"ix_device_tokens_user_id_{slug}",
            "device_tokens",
            ["user_id"],
            schema=schema,
        )
        op.create_index(
            f"ix_device_tokens_user_active_{slug}",
            "device_tokens",
            ["user_id", "is_active"],
            schema=schema,
        )


def downgrade() -> None:
    """Supprime la table ``device_tokens`` de chaque schema tenant existant."""
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index(f"ix_device_tokens_user_active_{slug}", table_name="device_tokens", schema=schema)
        op.drop_index(f"ix_device_tokens_user_id_{slug}", table_name="device_tokens", schema=schema)
        op.drop_table("device_tokens", schema=schema)
