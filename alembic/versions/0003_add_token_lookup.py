"""add token_lookup to refresh_tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # [⚠️ PROD] ADD COLUMN nullable sans DEFAULT : opération instantanée sur PostgreSQL
    # (pas de réécriture de table). Les tokens existants conservent token_lookup=NULL
    # et restent révocables via le scan bcrypt de fallback dans refresh_token().
    op.add_column(
        "refresh_tokens",
        sa.Column("token_lookup", sa.String(64), nullable=True),
    )
    op.create_unique_constraint("uq_refresh_tokens_token_lookup", "refresh_tokens", ["token_lookup"])
    op.create_index("ix_refresh_tokens_token_lookup", "refresh_tokens", ["token_lookup"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_token_lookup", table_name="refresh_tokens")
    op.drop_constraint("uq_refresh_tokens_token_lookup", "refresh_tokens", type_="unique")
    op.drop_column("refresh_tokens", "token_lookup")
