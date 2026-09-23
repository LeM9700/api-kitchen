"""Fix kod-mome tenant currency from EUR (wrong) to RSD (real menu prices)

Revision ID: 0064
Revises: 0062
Create Date: 2026-09-23

Contexte : le tenant kod-mome a ses vrais prix catalogue en RSD (dinar serbe
-- voir app-client/lib/core/utils/price_formatter.dart), mais
TenantConfig.currency etait reste a "EUR" (valeur par defaut posee par la
migration 0059) car RSD n'etait pas encore dans SUPPORTED_CURRENCIES a
l'epoque. Consequence en prod : la conversion indicative multi-devise
(``?display_currency=``, voir catalog/router.py::_apply_display_currency)
traitait les montants RSD comme des EUR, produisant des prix affiches ~117x
trop eleves (ex: 1100 RSD -> "1260.93 USD" au lieu de "~10.73 USD").

Meme garde-fou que admin/tenants/service.py::update_config : on ne change
jamais la devise d'un tenant qui a deja des paiements reels, pour ne pas
desynchroniser un paiement Stripe historique d'une devise de config changee
apres coup. Ce tenant est tres recent (catalogue mis en ligne le
2026-09-13) et n'a jamais eu de paiement reel au moment de cette migration --
verifie ci-dessous avant toute ecriture, pas suppose.
"""

from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0062"
branch_labels = None
depends_on = None

_SCHEMA = "tenant_kod-mome"
_REAL_PAYMENT_STATUSES_SQL = "'paid', 'partially_refunded', 'refunded'"


def _schema_exists(bind) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": _SCHEMA},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _schema_exists(bind):
        # Environnement sans ce tenant (tests, autre deploiement) -- no-op.
        return

    has_real_payment = bind.execute(
        sa.text(
            f'SELECT 1 FROM "{_SCHEMA}".payments '
            f"WHERE status IN ({_REAL_PAYMENT_STATUSES_SQL}) LIMIT 1"
        )
    ).first()
    if has_real_payment is not None:
        # Paiement reel deja present : ne jamais changer la devise sous ses
        # pieds, meme garde-fou que le service applicatif. Necessite alors
        # une correction manuelle/reconciliation, pas cette migration.
        return

    bind.execute(sa.text(f'UPDATE "{_SCHEMA}".tenant_config SET currency = \'RSD\''))


def downgrade() -> None:
    bind = op.get_bind()
    if not _schema_exists(bind):
        return
    bind.execute(sa.text(f'UPDATE "{_SCHEMA}".tenant_config SET currency = \'EUR\''))
