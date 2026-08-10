"""Add refresh_token_encrypted and token_expires_at to public.pos_connections

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-10

Les hubs OAuth POS emettent quasi toujours un refresh_token en plus de
l'access_token -- sans le stocker, la connexion expirerait silencieusement
des l'expiration de l'access_token (souvent ~1h) sans mecanisme de
rafraichissement. token_expires_at permet de savoir quand rafraichir.

Schema public (donnee d'infrastructure), coherent avec 0044 : pas de boucle
sur public.tenants, pas de duplication necessaire dans
_TENANT_DDL_STATEMENTS (app/modules/auth/service.py) -- cette regle ne
concerne que les tables par schema tenant.
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pos_connections",
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        schema="public",
    )
    op.add_column(
        "pos_connections",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("pos_connections", "token_expires_at", schema="public")
    op.drop_column("pos_connections", "refresh_token_encrypted", schema="public")
