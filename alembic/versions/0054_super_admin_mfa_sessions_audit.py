"""Harden public.super_admins: MFA, dedicated sessions, global revocation, audit

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-07

[SECURITE] Durcissement de l'authentification Super Admin plateforme (compte
public.super_admins, independant des tenants -- voir app/modules/super_admin/).
Contexte : app/modules/super_admin/router.py::super_admin_login n'imposait
aucun MFA et emettait un JWT sans jti (non revocable individuellement) ni
notion de session -- un mot de passe seul suffisait pour acceder a des
capacites cross-tenant (impersonation, suspension de tenant, listing
d'utilisateurs de tous les tenants).

Tables et colonnes ajoutees (schema public uniquement -- ce compte n'a jamais
ete tenant-scoped, donc aucune boucle sur public.tenants n'est necessaire ici,
contrairement aux migrations qui touchent les schemas tenant_*) :

- public.super_admins : + mfa_secret_encrypted (Fernet, JAMAIS en clair),
  mfa_enabled, auth_version (revocation globale instantanee de toutes les
  sessions/tokens emis, sans dependre de la deny-list Redis).
- public.super_admin_recovery_codes : codes de secours a usage unique, HACHES
  (bcrypt, jamais le code en clair), consommation tracee via used_at.
- public.super_admin_sessions : une ligne par session (refresh token actif),
  cle primaire = sid injecte dans les JWT access/refresh -- permet la
  revocation individuelle ou globale sans attendre l'expiration du token.
- public.platform_audit_logs : journal d'audit central, persistant,
  independant des schemas tenant -- connexions, echecs MFA, impersonation,
  suspension de tenant, revocation de session. Ne stocke jamais de secret
  (voir app/core/audit/platform_audit.py pour la politique de redaction).
"""

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.super_admins
            ADD COLUMN IF NOT EXISTS mfa_secret_encrypted TEXT NULL,
            ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 1
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.super_admin_recovery_codes (
            id              SERIAL PRIMARY KEY,
            super_admin_id  INTEGER NOT NULL REFERENCES public.super_admins(id) ON DELETE CASCADE,
            code_hash       VARCHAR(255) NOT NULL,
            used_at         TIMESTAMPTZ NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_admin_recovery_codes_admin "
        "ON public.super_admin_recovery_codes (super_admin_id) WHERE used_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.super_admin_sessions (
            id                     VARCHAR(36) PRIMARY KEY,
            super_admin_id         INTEGER NOT NULL REFERENCES public.super_admins(id) ON DELETE CASCADE,
            refresh_token_lookup   VARCHAR(64) NOT NULL UNIQUE,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at             TIMESTAMPTZ NOT NULL,
            revoked_at             TIMESTAMPTZ NULL,
            last_used_at           TIMESTAMPTZ NULL,
            ip_address             VARCHAR(45) NULL,
            user_agent             VARCHAR(512) NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_admin_sessions_admin "
        "ON public.super_admin_sessions (super_admin_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.platform_audit_logs (
            id                      SERIAL PRIMARY KEY,
            event_type              VARCHAR(64) NOT NULL,
            actor_super_admin_id    INTEGER NULL REFERENCES public.super_admins(id) ON DELETE SET NULL,
            actor_email             VARCHAR(255) NULL,
            target_type             VARCHAR(64) NULL,
            target_id               VARCHAR(128) NULL,
            ip_address              VARCHAR(45) NULL,
            user_agent              VARCHAR(512) NULL,
            metadata                JSONB NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_platform_audit_logs_event_type "
        "ON public.platform_audit_logs (event_type)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_platform_audit_logs_actor "
        "ON public.platform_audit_logs (actor_super_admin_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_platform_audit_logs_created_at "
        "ON public.platform_audit_logs (created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.platform_audit_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS public.super_admin_sessions CASCADE")
    op.execute("DROP TABLE IF EXISTS public.super_admin_recovery_codes CASCADE")
    op.execute(
        """
        ALTER TABLE public.super_admins
            DROP COLUMN IF EXISTS mfa_secret_encrypted,
            DROP COLUMN IF EXISTS mfa_enabled,
            DROP COLUMN IF EXISTS auth_version
        """
    )
