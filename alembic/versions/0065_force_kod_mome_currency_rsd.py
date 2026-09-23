"""Force kod-mome tenant currency to RSD (confirmed test-mode payments only)

Revision ID: 0065
Revises: 0064
Create Date: 2026-09-23

Contexte : la migration 0064 a correctement refuse de changer la devise de
kod-mome (garde-fou "paiement reel existant", meme logique que
admin/tenants/service.py::update_config) car 10 lignes payments avaient un
status dans ('paid', 'partially_refunded', 'refunded'). Verification manuelle
(dashboard Stripe, confirme par l'operateur du tenant) : ces 10 paiements sont
tous en mode TEST Stripe, aucun client reel n'a jamais ete facture. Le
garde-fou est donc leve intentionnellement ici, une seule fois, pour ce
tenant precis -- ce n'est PAS un changement de comportement du garde-fou
lui-meme (update_config continue de le respecter pour toute future demande
via l'API self-service).
"""

from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

_SCHEMA = "tenant_kod-mome"


def _schema_exists(bind) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": _SCHEMA},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _schema_exists(bind):
        return
    bind.execute(sa.text(f'UPDATE "{_SCHEMA}".tenant_config SET currency = \'RSD\''))


def downgrade() -> None:
    bind = op.get_bind()
    if not _schema_exists(bind):
        return
    bind.execute(sa.text(f'UPDATE "{_SCHEMA}".tenant_config SET currency = \'EUR\''))
