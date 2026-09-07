"""Journal d'audit central plateforme (public.platform_audit_logs).

[SECURITE] Contrairement au reste de la base (login_audit.py, l'ancien log
d'impersonation dans tenant_config_audits -- tous best-effort, exceptions
avalees), l'ecriture d'un evenement d'audit issu d'une action privilegiee
Super Admin est FAIL-CLOSED : si l'insertion echoue, l'exception se propage
et l'appelant (login, MFA, impersonation, revocation de session, creation ou
suspension de tenant) doit rollback plutot que de delivrer un acces non
trace. Voir tests/test_platform_audit_security.py.

Appeler ``record_platform_audit_event()`` DANS LA MEME session/transaction que
l'action auditee, AVANT le commit -- si l'insertion echoue, le rollback de la
transaction appelante annule aussi l'action (aucun token/session ne peut donc
etre delivre sans que sa trace d'audit soit deja ecrite).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.super_admin.models import PlatformAuditLog

# [SECURITE] Redaction defensive : ces cles ne doivent JAMAIS apparaitre dans
# metadata, meme si un appelant les y place par erreur. Ne remplace pas la
# discipline des appelants (ne jamais construire metadata a partir d'un
# payload de requete brut) -- c'est un filet de securite supplementaire.
_FORBIDDEN_METADATA_KEYS = frozenset({
    "password",
    "password_hash",
    "mfa_code",
    "totp_code",
    "recovery_code",
    "secret",
    "mfa_secret",
    "mfa_secret_encrypted",
    "token",
    "access_token",
    "refresh_token",
    "jwt",
    "authorization",
    "token_hash",
    "token_lookup",
})


def _redact(value: Any) -> Any:
    """Retire recursivement toute valeur associee a une cle sensible."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _FORBIDDEN_METADATA_KEYS else _redact(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def record_platform_audit_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor_super_admin_id: int | None,
    actor_email: str | None,
    target_type: str | None = None,
    target_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insere une ligne d'audit plateforme -- FAIL-CLOSED (voir docstring module).

    N'appelle jamais ``session.commit()`` : l'appelant controle la transaction,
    pour que l'echec de cet insert annule aussi l'action privilegiee auditee.

    Args:
        session: Session SQLAlchemy async, dans la MEME transaction que
            l'action auditee (public.platform_audit_logs).
        event_type: Identifiant court de l'evenement (ex. "login_success",
            "mfa_failed", "impersonation_started", "session_revoked").
        actor_super_admin_id: ID du super-admin a l'origine de l'action, ou
            None si l'acteur n'a pas pu etre resolu (ex. login avec un email
            inexistant).
        actor_email: Email declare par l'acteur (utile meme quand
            actor_super_admin_id est None).
        target_type: Type de la ressource ciblee (ex. "tenant", "session").
        target_id: Identifiant de la ressource ciblee (slug, sid...).
        ip_address: IP client (voir get_client_ip).
        user_agent: User-Agent HTTP.
        metadata: Contexte additionnel -- jamais de secret (redige
            defensivement, voir _FORBIDDEN_METADATA_KEYS ; les appelants ne
            doivent de toute facon jamais y placer un secret).

    Raises:
        Exception: toute erreur SQLAlchemy/DB est propagee telle quelle --
            c'est le comportement voulu (fail-closed).
    """
    session.add(
        PlatformAuditLog(
            event_type=event_type,
            actor_super_admin_id=actor_super_admin_id,
            actor_email=actor_email,
            target_type=target_type,
            target_id=target_id,
            ip_address=ip_address,
            user_agent=user_agent,
            event_metadata=_redact(metadata) if metadata else None,
        )
    )
    await session.flush()
