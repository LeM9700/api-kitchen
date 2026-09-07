"""Tests de la politique d'audit plateforme (public.platform_audit_logs).

Couvre deux garanties explicitement requises pour le Prompt 04 :
1. Rédaction -- aucun secret (mot de passe, code TOTP, code de récupération,
   secret MFA, JWT/refresh token) ne doit jamais atteindre la colonne
   ``metadata``, même si un appelant en place un par erreur.
2. Fail-closed -- si l'écriture d'un événement d'audit lié à une action
   privilégiée échoue, l'action elle-même (ici : le login, qui délivre un
   token) doit être bloquée plutôt que de réussir sans laisser de trace.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from app.core.audit.platform_audit import _redact, record_platform_audit_event
from app.core.auth.security import get_password_hash
from app.core.database import get_public_session
from app.modules.super_admin.models import PlatformAuditLog, SuperAdmin


def test_redact_strips_known_secret_keys():
    raw = {
        "password": "hunter2",
        "mfa_code": "123456",
        "totp_code": "654321",
        "recovery_code": "AB12CD34",
        "secret": "JBSWY3DPEHPK3PXP",
        "refresh_token": "eyJ...",
        "nested": {"authorization": "Bearer xyz", "safe_field": "kept"},
        "safe_field": "kept",
    }

    redacted = _redact(raw)

    assert redacted["password"] == "[REDACTED]"
    assert redacted["mfa_code"] == "[REDACTED]"
    assert redacted["totp_code"] == "[REDACTED]"
    assert redacted["recovery_code"] == "[REDACTED]"
    assert redacted["secret"] == "[REDACTED]"
    assert redacted["refresh_token"] == "[REDACTED]"
    assert redacted["nested"]["authorization"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "kept"
    assert redacted["safe_field"] == "kept"


async def test_record_platform_audit_event_persists_redacted_metadata(unique_slug):
    email = f"audit-{unique_slug}@test.com"
    async with get_public_session() as session:
        admin = SuperAdmin(email=email, password_hash="unused-hash")
        session.add(admin)
        await session.flush()

        await record_platform_audit_event(
            session,
            event_type="login_success",
            actor_super_admin_id=admin.id,
            actor_email=admin.email,
            metadata={"mfa_method": "totp", "password": "should-never-be-here"},
        )
        await session.commit()
        admin_id = admin.id

    async with get_public_session() as session:
        row = await session.scalar(
            select(PlatformAuditLog).where(PlatformAuditLog.actor_super_admin_id == admin_id)
        )
        assert row is not None
        assert row.event_metadata["mfa_method"] == "totp"
        assert row.event_metadata["password"] == "[REDACTED]"


async def test_login_success_is_blocked_if_audit_write_fails(client, unique_slug):
    """[SECURITE] Fail-closed : si l'insertion d'audit echoue, AUCUN token
    n'est delivre -- l'exception remonte plutot que d'etre avalee. Contraste
    volontaire avec le reste de la base (login_audit.py Mongo, best-effort)."""
    email = f"audit-failclosed-{unique_slug}@test.com"
    password = "correct horse battery staple"
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, :hash, true)"
            ),
            {"email": email, "hash": get_password_hash(password)},
        )
        await session.commit()

    # [SECURITE] L'exception simulee remonte telle quelle (ASGITransport la
    # re-leve cote test) -- jamais de reponse 200 avec un token. En
    # production, le handler d'exceptions generique de FastAPI la traduirait
    # en 500 ; ce qui compte ici est qu'aucun chemin ne l'avale silencieusement
    # pour renvoyer un acces malgre l'echec d'audit.
    with patch(
        "app.modules.super_admin.service.record_platform_audit_event",
        side_effect=RuntimeError("simulated audit outage"),
    ), pytest.raises(RuntimeError, match="simulated audit outage"):
        await client.post(
            "/api/v1/super-admin/login", json={"email": email, "password": password}
        )

    # Aucune trace de connexion reussie n'a ete laissee : ni token utilisable,
    # ni derive d'etat (last_login_at inchange), puisque toute la transaction
    # (y compris l'ecriture last_login_at) a ete annulee avec l'audit.
    async with get_public_session() as session:
        row = await session.execute(
            text("SELECT last_login_at FROM public.super_admins WHERE email = :email"),
            {"email": email},
        )
        assert row.scalar_one() is None
