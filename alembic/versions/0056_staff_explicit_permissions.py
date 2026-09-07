"""Make permissions explicit for historical staff accounts (least-privilege fix)

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Normalise ``permissions=NULL`` en ``[]`` pour tous les comptes staff existants.

    [🔒 SÉCURITÉ] Avant ce correctif, ``permissions=NULL`` sur un compte
    ``role='staff'`` était traité par ``app.core.http.deps.has_permission``
    comme un accès total ("staff légataire non restreint durant le
    rollout"). Cette exception est supprimée : ``has_permission`` traite
    désormais ``permissions=NULL``/``[]`` comme AUCUN droit fin, pour tous
    les comptes, historiques compris. Sans cette migration, un compte staff
    historique perdrait silencieusement tout accès aux routes
    ``require_permission(...)`` au moment du déploiement du code applicatif
    -- cette migration rend le nouvel état explicite en base (liste vide),
    ce qui ne change pas le comportement observable (le déni était déjà
    l'intention de sécurité) mais documente en base l'état réel de chaque
    compte, prêt à être complété explicitement par un admin via
    ``PATCH /admin/users/{id}/permissions``.

    Idempotent : ne touche que les lignes où ``permissions IS NULL``.
    """
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        quoted = f'"{schema}"'
        bind.execute(
            sa.text(
                f"UPDATE {quoted}.users SET permissions = '[]'::json "
                f"WHERE role = 'staff' AND permissions IS NULL"
            )
        )


def downgrade() -> None:
    """Pas de downgrade utile : revenir à ``permissions=NULL`` réintroduirait

    la faille de moindre privilège corrigée par cette migration (voir
    ``app.core.http.deps.has_permission``). No-op intentionnel.
    """
    pass
