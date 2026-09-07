"""Logique métier de l'authentification Super Admin plateforme.

[SECURITE] Toute écriture ``platform_audit_logs`` d'une action privilégiée
(connexion réussie, MFA activé, refresh, révocation) est faite via
``record_platform_audit_event`` DANS LA MÊME transaction que l'action, avant
``session.commit()`` — si l'audit échoue, toute la transaction est annulée :
aucun token ni session n'est jamais délivré sans que sa trace soit déjà
écrite (voir app.core.audit.platform_audit).
"""

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import secrets
import uuid

import pyotp
import qrcode
from jwt.exceptions import PyJWTError as JWTError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.platform_audit import record_platform_audit_event
from app.core.auth.security import (
    DUMMY_HASH,
    compute_token_lookup,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.core.config import settings
from app.core.database import get_public_session
from app.core.http.errors import AppError
from app.core.services.crypto import (
    CryptoNotConfigured,
    decrypt_super_admin_mfa_secret,
    encrypt_super_admin_mfa_secret,
)
from app.modules.super_admin.models import SuperAdmin, SuperAdminRecoveryCode, SuperAdminSession

_RECOVERY_CODE_COUNT = 10


def _generate_recovery_codes(count: int = _RECOVERY_CODE_COUNT) -> list[str]:
    return [secrets.token_hex(4).upper() for _ in range(count)]


async def _verify_login_mfa(
    session: AsyncSession, admin: SuperAdmin, mfa_code: str | None
) -> tuple[bool, str | None]:
    """Vérifie un code TOTP ou, à défaut, un code de récupération.

    [SECURITE] ``with_for_update()`` verrouille les lignes candidates : deux
    requêtes concurrentes soumettant le MÊME code de récupération ne peuvent
    pas le consommer toutes les deux (la seconde relit ``used_at`` déjà posé
    par la première une fois le verrou libéré et ne trouve plus de ligne).

    Returns:
        Tuple ``(succès, méthode)`` — méthode vaut ``"totp"`` ou
        ``"recovery_code"`` pour l'audit, ``None`` si échec.
    """
    if not mfa_code:
        return False, None
    code = mfa_code.strip()

    if admin.mfa_secret_encrypted:
        try:
            secret = decrypt_super_admin_mfa_secret(admin.mfa_secret_encrypted)
        except CryptoNotConfigured:
            secret = None
        if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
            return True, "totp"

    result = await session.execute(
        select(SuperAdminRecoveryCode)
        .where(
            SuperAdminRecoveryCode.super_admin_id == admin.id,
            SuperAdminRecoveryCode.used_at.is_(None),
        )
        .with_for_update()
    )
    for recovery_code in result.scalars():
        if verify_password(code, recovery_code.code_hash):
            recovery_code.used_at = datetime.now(timezone.utc)
            await session.flush()
            return True, "recovery_code"

    return False, None


async def login(body, ip: str | None, user_agent: str | None) -> dict:
    """Authentifie un super-admin plateforme.

    [SECURITE] Migration progressive contrôlée : un compte actif qui n'a
    jamais activé son MFA obtient, avec mot de passe seul, un token
    d'enrôlement restreint (``role="super-admin-enrollment"``, 10 min, pas de
    refresh) qui n'ouvre AUCUNE capacité métier — seuls
    ``/super-admin/mfa/setup`` et ``/mfa/confirm`` l'acceptent (voir
    ``app.core.http.deps.get_current_user`` et
    ``app.core.auth.super_admin.validate_super_admin_session``). Un compte
    avec ``mfa_enabled=True`` ne peut plus jamais se connecter au mot de passe
    seul : ``mfa_code`` devient obligatoire.

    Raises:
        AppError: UNAUTHORIZED (401) si identifiants invalides, compte
            désactivé, ou code MFA manquant/invalide.
    """
    async with get_public_session() as session:
        admin = await session.scalar(select(SuperAdmin).where(SuperAdmin.email == body.email))

        # [SECURITE] Timing-safe : un hash bcrypt est toujours calculé, même
        # si l'email n'existe pas, pour ne pas révéler l'existence d'un compte.
        stored_hash = admin.password_hash if admin else DUMMY_HASH
        password_ok = verify_password(body.password, stored_hash)

        if not admin or not password_ok:
            await record_platform_audit_event(
                session,
                event_type="login_failed",
                actor_super_admin_id=admin.id if admin else None,
                actor_email=body.email,
                ip_address=ip,
                user_agent=user_agent,
                metadata={"reason": "invalid_credentials"},
            )
            await session.commit()
            raise AppError("UNAUTHORIZED", "Identifiants invalides.", 401)

        if not admin.is_active:
            await record_platform_audit_event(
                session,
                event_type="login_failed",
                actor_super_admin_id=admin.id,
                actor_email=admin.email,
                ip_address=ip,
                user_agent=user_agent,
                metadata={"reason": "account_disabled"},
            )
            await session.commit()
            raise AppError("UNAUTHORIZED", "Ce compte super-admin est désactivé.", 401)

        now = datetime.now(timezone.utc)

        if not admin.mfa_enabled:
            enrollment_ttl = timedelta(minutes=settings.super_admin_mfa_enrollment_token_minutes)
            token = create_access_token(
                {
                    "sub": str(admin.id),
                    "email": admin.email,
                    "role": "super-admin-enrollment",
                    "tenant_slug": None,
                    "tenant_id": None,
                    "auth_version": admin.auth_version,
                },
                expires_delta=enrollment_ttl,
            )
            admin.last_login_at = now
            await record_platform_audit_event(
                session,
                event_type="login_success",
                actor_super_admin_id=admin.id,
                actor_email=admin.email,
                ip_address=ip,
                user_agent=user_agent,
                metadata={"scope": "mfa_enrollment_required"},
            )
            await session.commit()
            return {
                "access_token": token,
                "refresh_token": None,
                "token_type": "bearer",
                "expires_in": int(enrollment_ttl.total_seconds()),
                "mfa_setup_required": True,
            }

        mfa_ok, mfa_method = await _verify_login_mfa(session, admin, body.mfa_code)
        if not mfa_ok:
            await record_platform_audit_event(
                session,
                event_type="mfa_failed",
                actor_super_admin_id=admin.id,
                actor_email=admin.email,
                ip_address=ip,
                user_agent=user_agent,
            )
            await session.commit()
            code = "MFA_REQUIRED" if not body.mfa_code else "INVALID_MFA_CODE"
            raise AppError(code, "Code MFA requis ou invalide.", 401, "mfa_code")

        sid = str(uuid.uuid4())
        access_ttl = timedelta(minutes=settings.jwt_access_expire_minutes)
        refresh_ttl = timedelta(days=settings.super_admin_refresh_expire_days)
        access = create_access_token(
            {
                "sub": str(admin.id),
                "email": admin.email,
                "role": "super-admin",
                "tenant_slug": None,
                "tenant_id": None,
                "sid": sid,
                "auth_version": admin.auth_version,
            },
            expires_delta=access_ttl,
        )
        refresh = create_refresh_token(
            {"sub": str(admin.id), "role": "super-admin", "sid": sid},
            expires_delta=refresh_ttl,
        )
        session.add(
            SuperAdminSession(
                id=sid,
                super_admin_id=admin.id,
                refresh_token_lookup=compute_token_lookup(refresh),
                expires_at=now + refresh_ttl,
                ip_address=ip,
                user_agent=user_agent,
            )
        )
        admin.last_login_at = now
        await record_platform_audit_event(
            session,
            event_type="login_success",
            actor_super_admin_id=admin.id,
            actor_email=admin.email,
            ip_address=ip,
            user_agent=user_agent,
            metadata={"mfa_method": mfa_method, "sid": sid},
        )
        await session.commit()
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": int(access_ttl.total_seconds()),
            "mfa_setup_required": False,
        }


async def setup_mfa(admin_id: int) -> dict:
    """Génère un nouveau secret TOTP + codes de récupération (non confirmé).

    Idempotent tant que le MFA n'est pas confirmé : rappeler cette fonction
    remplace le secret et les codes de récupération non utilisés précédents.
    """
    async with get_public_session() as session:
        admin = await session.get(SuperAdmin, admin_id)
        if admin is None or not admin.is_active:
            raise AppError("UNAUTHORIZED", "Super-admin introuvable.", 401)
        if admin.mfa_enabled:
            raise AppError("MFA_ALREADY_ENABLED", "Le MFA est déjà activé.", 409)

        secret = pyotp.random_base32()
        recovery_codes = _generate_recovery_codes()

        admin.mfa_secret_encrypted = encrypt_super_admin_mfa_secret(secret)

        await session.execute(
            delete(SuperAdminRecoveryCode).where(
                SuperAdminRecoveryCode.super_admin_id == admin_id,
                SuperAdminRecoveryCode.used_at.is_(None),
            )
        )
        for code in recovery_codes:
            session.add(
                SuperAdminRecoveryCode(super_admin_id=admin_id, code_hash=get_password_hash(code))
            )

        otpauth_uri = pyotp.TOTP(secret).provisioning_uri(
            name=admin.email, issuer_name="API Kitchen Super Admin"
        )
        qr_image = qrcode.make(otpauth_uri)
        buffer = BytesIO()
        qr_image.save(buffer, format="PNG")
        qr_code_png_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")

        await session.commit()
        return {
            "secret": secret,
            "otpauth_uri": otpauth_uri,
            "qr_code_png_base64": qr_code_png_base64,
            "recovery_codes": recovery_codes,
        }


async def confirm_mfa(admin_id: int, totp_code: str | None) -> dict:
    async with get_public_session() as session:
        admin = await session.get(SuperAdmin, admin_id)
        if admin is None or not admin.is_active:
            raise AppError("UNAUTHORIZED", "Super-admin introuvable.", 401)
        if admin.mfa_enabled:
            raise AppError("MFA_ALREADY_ENABLED", "Le MFA est déjà activé.", 409)
        if not admin.mfa_secret_encrypted:
            raise AppError("MFA_NOT_SETUP", "Appelez /mfa/setup avant /mfa/confirm.", 400)

        secret = decrypt_super_admin_mfa_secret(admin.mfa_secret_encrypted)
        if not totp_code or not pyotp.TOTP(secret).verify(totp_code.strip(), valid_window=1):
            raise AppError("INVALID_MFA_CODE", "Code MFA invalide.", 400, "totp_code")

        admin.mfa_enabled = True
        await record_platform_audit_event(
            session,
            event_type="mfa_enabled",
            actor_super_admin_id=admin.id,
            actor_email=admin.email,
        )
        await session.commit()
        return {"message": "MFA activé."}


async def refresh_session(token: str) -> dict:
    """Échange un refresh token super-admin valide contre une nouvelle paire.

    [SECURITE] Rotation atomique : la ligne ``super_admin_sessions`` n'est
    mise à jour QUE si ``refresh_token_lookup`` correspond encore exactement
    au token présenté (``WHERE refresh_token_lookup = :old_lookup``). Si deux
    requêtes concurrentes présentent le même refresh token, seule la première
    à committer trouve la ligne dans cet état — la seconde échoue avec 0 ligne
    affectée et reçoit 401, empêchant le rejeu d'un refresh token déjà tourné.
    """
    try:
        payload = decode_token(token)
    except JWTError as exc:
        raise AppError("UNAUTHORIZED", "Refresh token invalide.", 401) from exc

    if payload.get("type") != "refresh" or payload.get("role") != "super-admin":
        raise AppError("UNAUTHORIZED", "Refresh token invalide.", 401)

    sid = payload.get("sid")
    admin_id_str = payload.get("sub")
    if not sid or not admin_id_str:
        raise AppError("UNAUTHORIZED", "Refresh token invalide.", 401)

    old_lookup = compute_token_lookup(token)
    now = datetime.now(timezone.utc)

    async with get_public_session() as session:
        admin = await session.get(SuperAdmin, int(admin_id_str))
        if admin is None or not admin.is_active:
            raise AppError("UNAUTHORIZED", "Refresh token invalide.", 401)

        access_ttl = timedelta(minutes=settings.jwt_access_expire_minutes)
        refresh_ttl = timedelta(days=settings.super_admin_refresh_expire_days)
        new_access = create_access_token(
            {
                "sub": str(admin.id),
                "email": admin.email,
                "role": "super-admin",
                "tenant_slug": None,
                "tenant_id": None,
                "sid": sid,
                "auth_version": admin.auth_version,
            },
            expires_delta=access_ttl,
        )
        new_refresh = create_refresh_token(
            {"sub": str(admin.id), "role": "super-admin", "sid": sid},
            expires_delta=refresh_ttl,
        )

        result = await session.execute(
            update(SuperAdminSession)
            .where(
                SuperAdminSession.id == sid,
                SuperAdminSession.super_admin_id == admin.id,
                SuperAdminSession.refresh_token_lookup == old_lookup,
                SuperAdminSession.revoked_at.is_(None),
                SuperAdminSession.expires_at > now,
            )
            .values(
                refresh_token_lookup=compute_token_lookup(new_refresh),
                expires_at=now + refresh_ttl,
                last_used_at=now,
            )
        )
        if result.rowcount != 1:
            raise AppError(
                "UNAUTHORIZED", "Session révoquée, expirée ou refresh token déjà utilisé.", 401
            )

        await session.commit()
        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer",
            "expires_in": int(access_ttl.total_seconds()),
            "mfa_setup_required": False,
        }


async def logout(admin_id: int, sid: str | None) -> None:
    """Révoque la session courante (sid) d'un super-admin."""
    if not sid:
        return
    async with get_public_session() as session:
        await session.execute(
            update(SuperAdminSession)
            .where(SuperAdminSession.id == sid, SuperAdminSession.super_admin_id == admin_id)
            .values(revoked_at=datetime.now(timezone.utc))
        )
        admin = await session.get(SuperAdmin, admin_id)
        await record_platform_audit_event(
            session,
            event_type="session_revoked",
            actor_super_admin_id=admin_id,
            actor_email=admin.email if admin else None,
            target_type="session",
            target_id=sid,
        )
        await session.commit()


async def revoke_all_sessions(admin_id: int) -> None:
    """Révocation globale : incrémente ``auth_version`` et révoque toutes les sessions.

    [SECURITE] ``auth_version`` invalide instantanément tout access/refresh
    token déjà émis pour ce compte (voir ``validate_super_admin_session``),
    y compris ceux dont le ``jti`` n'est pas dans la deny-list Redis.
    """
    async with get_public_session() as session:
        admin = await session.get(SuperAdmin, admin_id)
        if admin is None:
            raise AppError("UNAUTHORIZED", "Super-admin introuvable.", 401)

        admin.auth_version += 1
        await session.execute(
            update(SuperAdminSession)
            .where(
                SuperAdminSession.super_admin_id == admin_id,
                SuperAdminSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await record_platform_audit_event(
            session,
            event_type="session_revoked_all",
            actor_super_admin_id=admin_id,
            actor_email=admin.email,
            metadata={"new_auth_version": admin.auth_version},
        )
        await session.commit()
