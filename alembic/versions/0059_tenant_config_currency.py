"""Add currency column to tenant_config

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-15

Contexte : la devise de facturation etait en dur ("EUR") dans le module
payments, sans aucun moyen pour un tenant de la configurer. Cette migration
ajoute TenantConfig.currency (ISO 4217, 3 caracteres, defaut "EUR" pour
preserver le comportement actuel de tous les tenants existants). La colonne
est ensuite verrouillee cote applicatif (admin/tenants/service.py) des qu'un
paiement reel existe pour le tenant, pour eviter toute incoherence entre des
paiements historiques et une devise de config changee apres coup.
"""

from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column(
            "tenant_config",
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "currency", schema=schema)
