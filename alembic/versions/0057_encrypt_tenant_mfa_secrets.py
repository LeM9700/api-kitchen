"""Encrypt at rest existing plaintext TOTP secrets on tenant users

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

# Un texte chiffre par Fernet commence toujours par ce prefixe base64 (octet
# de version 0x80). Sert a distinguer un secret deja chiffre (skip, migration
# idempotente) d'un secret TOTP base32 encore en clair (a chiffrer).
_FERNET_PREFIX = "gAAAAA"


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    """Chiffre en place les secrets TOTP existants (``users.mfa_secret``).

    [🔒 SÉCURITÉ] Avant ce correctif, ``users.mfa_secret`` était persisté en
    clair (secret TOTP base32). Cette migration le chiffre avec
    ``TENANT_MFA_ENCRYPTION_KEY`` (voir ``app.core.services.crypto``), sans
    jamais faire transiter le secret par un log ou un message d'erreur.

    [⚠️ PROD] N'exige la clé que si des lignes en clair existent réellement --
    une base de test/CI fraîche sans compte MFA n'a aucune ligne à chiffrer et
    ne doit pas échouer faute de clé configurée. En présence de lignes en
    clair sans clé configurée, la migration échoue explicitement plutôt que
    de laisser des secrets en clair en base (fail closed).

    Idempotente : une ligne dont la valeur commence déjà par le préfixe
    Fernet est laissée intacte (rejouer cette migration, ou un provisioning
    de tenant postérieur qui chiffre déjà à l'écriture, ne re-chiffre pas un
    texte déjà chiffré).
    """
    from cryptography.fernet import Fernet

    from app.core.config import settings

    bind = op.get_bind()
    slugs = _get_tenant_slugs(bind)

    pending: list[tuple[str, int, str]] = []  # (schema, user_id, plaintext_secret)
    for slug in slugs:
        schema = f"tenant_{slug}"
        rows = bind.execute(
            sa.text(f'SELECT id, mfa_secret FROM "{schema}".users WHERE mfa_secret IS NOT NULL')
        )
        for row in rows:
            if row.mfa_secret.startswith(_FERNET_PREFIX):
                continue
            pending.append((schema, row.id, row.mfa_secret))

    if not pending:
        return

    if not settings.tenant_mfa_encryption_key:
        raise RuntimeError(
            "TENANT_MFA_ENCRYPTION_KEY must be set before running migration 0057: "
            f"{len(pending)} plaintext MFA secret(s) found across tenant schemas."
        )

    fernet = Fernet(settings.tenant_mfa_encryption_key.encode())
    for schema, user_id, plaintext_secret in pending:
        encrypted = fernet.encrypt(plaintext_secret.encode()).decode()
        bind.execute(
            sa.text(f'UPDATE "{schema}".users SET mfa_secret = :enc WHERE id = :id'),
            {"enc": encrypted, "id": user_id},
        )


def downgrade() -> None:
    """Pas de downgrade : déchiffrer en masse réintroduirait des secrets TOTP

    en clair en base. No-op intentionnel (voir 0056 pour le même choix).
    """
    pass
