"""Add integration_mode to public.tenants and public.pos_connections table

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-05

Infrastructure pour le mode d'integration des tenants (P8) : autonome
(api-kitchen systeme de verite) vs connecte (caisse externe systeme de
verite). Tout est en schema public -- donnee d'infrastructure multi-tenant,
pas de donnee metier par schema locataire -- donc pas de boucle sur
public.tenants comme dans les migrations tenant-scoped (cf. 0043).

VARCHAR + CHECK constraint plutot qu'un type ENUM Postgres natif, coherent
avec le pattern deja en place sur ce projet pour les colonnes de statut
applicatives (plan, authority, order_type...), cf. 0043 et 0034 pour le
meme pattern.

Retrocompatibilite : integration_mode a un server_default 'standalone',
donc tous les tenants existants sont migres avec ce statut par defaut sans
qu'aucune ligne existante n'ait besoin d'etre touchee explicitement --
comportement inchange pour tous les tenants deja provisionnes.

Aucune route API n'est ajoutee ni modifiee dans cette migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "integration_mode",
            sa.String(16),
            nullable=False,
            server_default="standalone",
        ),
        schema="public",
    )
    op.create_check_constraint(
        "ck_tenants_integration_mode",
        "tenants",
        "integration_mode IN ('standalone', 'connected')",
        schema="public",
    )

    op.create_table(
        "pos_connections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer,
            sa.ForeignKey("public.tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        # Identifiant de l'etablissement cote caisse externe (ex: restaurant_id
        # POS, store_id...) -- format libre, defini par chaque provider.
        sa.Column("external_establishment_id", sa.String(255), nullable=False),
        # Chiffre au niveau applicatif avant insertion (hors perimetre de cette
        # migration -- aucun outillage de chiffrement n'existe encore dans le
        # repo). Nullable : une connexion peut exister en statut 'pending'
        # avant la fin du flux d'autorisation OAuth du provider.
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked')",
            name="ck_pos_connections_status",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_establishment_id",
            name="uq_pos_connections_provider_establishment",
        ),
        schema="public",
    )
    op.create_index(
        "ix_pos_connections_tenant_id",
        "pos_connections",
        ["tenant_id"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index("ix_pos_connections_tenant_id", table_name="pos_connections", schema="public")
    op.drop_table("pos_connections", schema="public")

    op.drop_constraint("ck_tenants_integration_mode", "tenants", schema="public", type_="check")
    op.drop_column("tenants", "integration_mode", schema="public")
