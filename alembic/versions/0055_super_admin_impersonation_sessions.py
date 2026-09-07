"""Add public.super_admin_impersonation_sessions -- persistent revocation source

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-07

[SECURITE] Correctif : la revocation d'un token d'impersonation reposait
UNIQUEMENT sur la deny-list Redis (voir 0054 + app/core/auth/impersonation.py).
Si Redis etait absent/indisponible, ``/impersonation/end`` pouvait ecrire un
audit "impersonation_ended" alors que le token restait en verite valide
jusqu'a son expiration -- la trace d'audit mentait sur l'etat reel.

``public.super_admin_impersonation_sessions`` devient la source de verite
persistante (PostgreSQL) pour l'etat d'une impersonation : chaque token
d'impersonation porte desormais un claim ``impersonation_id`` reference ici,
et ``validate_impersonation_token`` refuse tout token dont l'enregistrement
est absent, expire ou ``revoked_at`` non nul -- independamment de Redis, qui
reste seulement un accelerateur de revocation (defense en profondeur,
jamais la seule preuve).
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.super_admin_impersonation_sessions (
            id                  VARCHAR(36) PRIMARY KEY,
            super_admin_id      INTEGER NOT NULL REFERENCES public.super_admins(id) ON DELETE CASCADE,
            source_sid          VARCHAR(36) NOT NULL REFERENCES public.super_admin_sessions(id) ON DELETE CASCADE,
            tenant_slug         VARCHAR(255) NOT NULL,
            issued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at          TIMESTAMPTZ NOT NULL,
            revoked_at          TIMESTAMPTZ NULL,
            reason              VARCHAR(64) NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_admin_impersonation_sessions_admin "
        "ON public.super_admin_impersonation_sessions (super_admin_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_admin_impersonation_sessions_source_sid "
        "ON public.super_admin_impersonation_sessions (source_sid)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.super_admin_impersonation_sessions CASCADE")
