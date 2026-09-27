"""Document ingredient batch primary DLC requirement

Revision ID: 0069
Revises: 0068
Create Date: 2026-09-27

Phase DLC securite alimentaire:
- New batch creation is blocked at API/service level without expires_at.
- Existing rows may still have expires_at NULL. Those legacy rows must be
  surfaced as "a regulariser" and excluded from usable DLC stock in later
  phases, not silently marked compliant by migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'
        bind.execute(
            sa.text(
                f"""
                COMMENT ON COLUMN {quoted}.ingredient_batches.expires_at IS
                'DLC primaire du lot. Obligatoire pour les nouveaux lots via API; NULL legacy = a regulariser avec blocage operationnel.'
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'
        bind.execute(
            sa.text(
                f"""
                COMMENT ON COLUMN {quoted}.ingredient_batches.expires_at IS NULL
                """
            )
        )

