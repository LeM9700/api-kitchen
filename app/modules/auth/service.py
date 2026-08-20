import base64
from io import BytesIO
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import Base, engine, get_public_session, get_tenant_session, tenant_schema_name
from app.core.database import tenant_models  # noqa: F401
from app.core.http.deps import get_client_ip
from app.core.http.errors import AppError
from app.core.auth.security import (
    DUMMY_HASH,
    compute_token_lookup,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.core.tenancy.tenant import create_tenant_schema
from app.modules.auth.models import RefreshToken, User


async def _provision_tenant_schema(conn, slug: str) -> None:
    """Cree toutes les tables applicatives dans le schema tenant depuis les
    modeles SQLAlchemy (``Base.metadata``) — meme source de verite que les
    migrations Alembic (voir ``app.core.database.tenant_models``) — puis
    seed les donnees applicatives minimales (etablissement HR par defaut).

    Args:
        conn: Connexion SQLAlchemy async deja ouverte (dans une transaction).
        slug: Slug tenant valide, utilise pour construire le nom du schema.
    """
    schema = tenant_schema_name(slug)
    # Pas de fallback ", public" ici : Base.metadata.create_all(checkfirst=True)
    # resout les noms de table non qualifies via le search_path. public contient
    # des tables historiques homonymes (users, orders, products...) -- cf.
    # migration 0002 -- donc un fallback public ferait croire a tort que les
    # tables du tenant existent deja et create_all() ne les creerait jamais.
    await conn.execute(text(f'SET search_path TO "{schema}"'))
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn))
    await conn.execute(
        text(
            """INSERT INTO establishments (name, timezone)
               SELECT 'Établissement principal', 'Europe/Paris'
               WHERE NOT EXISTS (SELECT 1 FROM establishments)"""
        )
    )
    await conn.execute(
        text(
            """INSERT INTO establishment_hr_config (establishment_id)
               SELECT id FROM establishments
               WHERE id NOT IN (SELECT establishment_id FROM establishment_hr_config)"""
        )
    )
    await conn.execute(text("SET search_path TO public"))


async def _create_tenant_tables(tenant_slug: str) -> None:
    """Wrapper transactionnel autour de _provision_tenant_schema.

    Args:
        tenant_slug: Slug tenant dont le schema doit etre provisionne.
    """
    async with engine.begin() as conn:
        await _provision_tenant_schema(conn, tenant_slug)


async def register(body, arq_pool=None) -> tuple[User, str, str, int]:
    """Cree un nouveau tenant et son premier utilisateur admin.

    Genere un token de verification email (UUID4, expiry 24h) et enqueue
    send_verification_email si arq_pool est fourni.

    Args:
        body: Payload RegisterRequest valide par Pydantic.
        arq_pool: Pool arq optionnel pour l'envoi de l'email de verification.

    Returns:
        Tuple (user, access_token, refresh_token, session_id).

    Raises:
        AppError: TENANT_EXISTS (409) si le slug est deja pris.
    """
    async with get_public_session() as session:
        existing = await session.scalar(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        if existing:
            raise AppError("TENANT_EXISTS", "Tenant already exists", 409, "tenant_slug")
        row = await session.execute(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, 'starter') RETURNING id"
            ),
            {"slug": body.tenant_slug, "name": body.tenant_name},
        )
        tenant_id = row.scalar_one()
        await session.commit()

    await create_tenant_schema(body.tenant_slug)
    await _create_tenant_tables(body.tenant_slug)
    async with get_tenant_session(body.tenant_slug) as session:
        from app.modules.catalog.allergen.allergen_service import seed_regulatory_allergens

        await seed_regulatory_allergens(session)

    verification_token = str(uuid.uuid4())
    verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    async with get_tenant_session(body.tenant_slug) as session:
        # Vérification email unique avant insert — la contrainte DB UNIQUE catcherait
        # l'IntegrityError mais retournerait un 500 sans ce check explicite.
        existing_user = await session.scalar(
            select(User).where(User.email == body.email)
        )
        if existing_user is not None:
            raise AppError("EMAIL_ALREADY_EXISTS", "Email already registered", 409, "email")

        user = User(
            email=body.email,
            password_hash=get_password_hash(body.password),
            full_name=body.full_name,
            role="admin",
            email_verification_token=verification_token,
            email_verification_expires_at=verification_expires_at,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        access, refresh, session_id = await issue_tokens(session, user, tenant_id, body.tenant_slug)
        await session.commit()

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_verification_email",
                tenant_slug=body.tenant_slug,
                user_id=user.id,
                token=verification_token,
            )
        except Exception:
            pass  # Non critique : le user peut demander un renvoi

    return user, access, refresh, session_id


async def verify_email(token: str, tenant_slug: str) -> dict:
    """Marque l'email d'un utilisateur comme verifie a partir du token de confirmation.

    Args:
        token: Token UUID4 recu en query param depuis le lien d'email.
        tenant_slug: Slug du tenant (passe en query param dans l'URL de verification).

    Returns:
        Dict avec message de confirmation et email verifie.

    Raises:
        AppError: INVALID_TOKEN (400) si le token est invalide, introuvable ou expire.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.scalar(
            select(User).where(User.email_verification_token == token)
        )
        if user is None:
            raise AppError("INVALID_TOKEN", "Verification token invalid or already used", 400)

        now = datetime.now(timezone.utc)
        if user.email_verification_expires_at is None or user.email_verification_expires_at < now:
            raise AppError("INVALID_TOKEN", "Verification token has expired", 400)

        user.email_verified_at = now
        user.email_verification_token = None
        user.email_verification_expires_at = None
        await session.commit()
        return {"message": "Email verified successfully", "email": user.email}


def _generate_backup_codes(count: int = 10) -> list[str]:
    return [secrets.token_hex(4).upper() for _ in range(count)]


def _hash_backup_codes(codes: list[str]) -> list[str]:
    return [get_password_hash(code) for code in codes]


def _verify_totp(secret: str, code: str | None) -> bool:
    if not code:
        return False
    import pyotp

    return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=1))


def _build_mfa_payload(user: User, secret: str, backup_codes: list[str]) -> dict:
    import pyotp
    import qrcode

    otpauth_uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name="Pizzeria API",
    )
    qr_image = qrcode.make(otpauth_uri)
    buffer = BytesIO()
    qr_image.save(buffer, format="PNG")
    qr_code_png_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "secret": secret,
        "otpauth_uri": otpauth_uri,
        "qr_code_png_base64": qr_code_png_base64,
        "backup_codes": backup_codes,
    }


async def setup_mfa(tenant_slug: str, user_id: int) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA setup is reserved to admin/super-admin users", 403)
        if user.mfa_enabled:
            raise AppError("MFA_ALREADY_ENABLED", "MFA is already enabled", 409)

        import pyotp

        secret = pyotp.random_base32()
        backup_codes = _generate_backup_codes()
        user.mfa_secret = secret
        user.mfa_enabled = False
        user.mfa_backup_codes = _hash_backup_codes(backup_codes)
        await session.commit()
        await session.refresh(user)
        return _build_mfa_payload(user, secret, backup_codes)


async def confirm_mfa(tenant_slug: str, user_id: int, totp_code: str | None) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA confirmation is reserved to admin/super-admin users", 403)
        if not user.mfa_secret or not _verify_totp(user.mfa_secret, totp_code):
            raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 400, "totp_code")

        user.mfa_enabled = True
        await session.commit()
        return {"message": "MFA enabled"}


async def regenerate_mfa_backup_codes(
    tenant_slug: str,
    user_id: int,
    totp_code: str | None,
) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA backup codes are reserved to admin/super-admin users", 403)
        if not user.mfa_enabled or not user.mfa_secret:
            raise AppError("MFA_NOT_ENABLED", "MFA is not enabled", 400)
        if not _verify_totp(user.mfa_secret, totp_code):
            raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 400, "totp_code")

        backup_codes = _generate_backup_codes()
        user.mfa_backup_codes = _hash_backup_codes(backup_codes)
        await session.commit()
        return {"backup_codes": backup_codes}


async def _verify_login_mfa(session: AsyncSession, user: User, mfa_code: str | None) -> None:
    if not mfa_code:
        raise AppError("MFA_REQUIRED", "MFA code required", 401, "mfa_code")
    if not user.mfa_secret:
        raise AppError("MFA_REQUIRED", "MFA setup incomplete", 401, "mfa_code")

    if _verify_totp(user.mfa_secret, mfa_code):
        return

    backup_hashes = list(user.mfa_backup_codes or [])
    for index, hashed_code in enumerate(backup_hashes):
        if verify_password(mfa_code.strip(), hashed_code):
            user.mfa_backup_codes = backup_hashes[:index] + backup_hashes[index + 1 :]
            await session.flush()
            return

    raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 401, "mfa_code")


async def authenticate(
    session: AsyncSession,
    tenant_id: int,
    tenant_slug: str,
    email: str,
    password: str,
    mfa_code: str | None = None,
) -> tuple[User, str, str, int]:
    """Authentifie un utilisateur de facon timing-safe.

    Si l'email est introuvable, un bcrypt dummy est quand meme calcule pour que
    le temps de reponse soit identique (evite le timing oracle sur l'existence
    des comptes).

    [SECURITE] pwd_context.verify utilise une comparaison a temps constant en
    interne. Le dummy_verify sert uniquement a maintenir la duree de traitement.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        tenant_id: Identifiant numerique du tenant.
        tenant_slug: Slug tenant (pour le payload JWT).
        email: Email soumis par le client.
        password: Mot de passe en clair soumis par le client.

    Returns:
        Tuple (user, access_token, refresh_token, session_id).

    Raises:
        AppError: INVALID_CREDENTIALS (401) si email ou mot de passe invalide.
    """
    user = await session.scalar(
        select(User).where(User.email == email, User.is_active.is_(True))
    )

    # [SECURITE] Timing-safe : si user introuvable, on execute quand meme bcrypt
    # pour ne pas reveler via difference de temps qu'un email n'existe pas.
    if user is None:
        verify_password(password, DUMMY_HASH)  # dummy -- resultat intentionnellement ignore
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    if not verify_password(password, user.password_hash):
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    if user.role in ("super-admin", "admin") and user.mfa_enabled:
        await _verify_login_mfa(session, user, mfa_code)

    access, refresh, session_id = await issue_tokens(session, user, tenant_id, tenant_slug)
    await session.commit()
    return user, access, refresh, session_id


async def issue_tokens(
    session: AsyncSession,
    user: User,
    tenant_id: int,
    tenant_slug: str,
    request=None,  # Optional FastAPI Request for user_agent/ip_address
) -> tuple[str, str, int]:
    """Genere une paire access/refresh token et persiste le refresh en base.

    Le refresh token est stocke sous deux formes :
    - token_hash : bcrypt pour la verification securisee.
    - token_lookup : HMAC-SHA256 indexe pour le lookup O(1).

    Args:
        session: Session SQLAlchemy async (flush/commit a la charge du caller).
        user: Utilisateur pour lequel les tokens sont emis.
        tenant_id: Identifiant du tenant (inclus dans le payload JWT).
        tenant_slug: Slug du tenant (inclus dans le payload JWT).
        request: Requete FastAPI optionnelle pour extraire user_agent et ip_address.

    Returns:
        Tuple (access_token, refresh_token, session_id) — session_id est l'ID
        de la ligne refresh_token inseree.
    """
    user_agent = None
    ip_address = None
    if request is not None:
        user_agent = request.headers.get("user-agent", "")[:512] or None
        ip_address = get_client_ip(request)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "permissions": user.permissions,
        "tenant_id": tenant_id,
        "tenant_slug": tenant_slug,
        "must_change_password": user.must_change_password,
    }
    access = create_access_token(payload)
    refresh = create_refresh_token(payload)
    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=get_password_hash(refresh),
        token_lookup=compute_token_lookup(refresh),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    session.add(refresh_row)
    await session.flush()  # populate refresh_row.id
    return access, refresh, refresh_row.id


async def login(body) -> tuple[User, str, str, int]:
    async with get_public_session() as public:
        result = await public.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise AppError("TENANT_NOT_FOUND", "Tenant not found", 404, "tenant_slug")
    async with get_tenant_session(body.tenant_slug) as session:
        return await authenticate(
            session,
            tenant_id,
            body.tenant_slug,
            body.email,
            body.password,
            mfa_code=body.mfa_code,
        )


async def refresh_token(token: str) -> dict:
    """Echange un refresh token valide contre une nouvelle paire de tokens.

    Strategie de lookup en deux phases :
    1. Calcul du token_lookup (HMAC-SHA256) -> SELECT WHERE token_lookup = ? -> O(1).
    2. Fallback O(n*bcrypt) pour les tokens sans token_lookup (pre-migration 0003).
    3. Verification bcrypt sur le seul enregistrement trouve.

    [SECURITE] Le token revoque est marque revoked_at avant l'emission du nouveau
    (rotation monotone). En cas d'erreur, le rollback annule la revocation.

    Args:
        token: Refresh token JWT en clair extrait du corps de la requete.

    Returns:
        Dictionnaire {"access_token": str, "refresh_token": str}.

    Raises:
        AppError: INVALID_TOKEN (401) si le token est invalide, expire ou revoque.
        AppError: UNAUTHORIZED (401) si l'utilisateur associe est introuvable.
    """
    try:
        payload = decode_token(token)
    except Exception as exc:
        raise AppError("INVALID_TOKEN", "Refresh token is invalid", 401) from exc
    if payload.get("type") != "refresh":
        raise AppError("INVALID_TOKEN", "Refresh token required", 401)

    tenant_slug = payload["tenant_slug"]
    user_id = int(payload["sub"])
    lookup = compute_token_lookup(token)

    async with get_tenant_session(tenant_slug) as session:
        # Phase 1 : lookup O(1) via HMAC index.
        current = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_lookup == lookup,
                RefreshToken.revoked_at.is_(None),
            )
        )

        # Phase 2 : fallback O(n*bcrypt) pour les tokens sans token_lookup (pre-0003).
        if current is None:
            rows = await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.token_lookup.is_(None),
                    RefreshToken.revoked_at.is_(None),
                )
            )
            candidates = [r for r in rows.scalars() if verify_password(token, r.token_hash)]
            if not candidates:
                raise AppError("INVALID_TOKEN", "Refresh token is invalid or revoked", 401)
            current = candidates[0]
        else:
            if not verify_password(token, current.token_hash):
                raise AppError("INVALID_TOKEN", "Refresh token is invalid or revoked", 401)

        current.revoked_at = datetime.now(timezone.utc)
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        access, refresh, session_id = await issue_tokens(session, user, payload["tenant_id"], tenant_slug)
        await session.commit()
        return {"access_token": access, "refresh_token": refresh, "session_id": session_id}


async def logout(token: str, tenant_slug: str, user_id: int) -> None:
    async with get_tenant_session(tenant_slug) as session:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def get_sessions(
    user_id: int,
    tenant_slug: str,
    current_session_id: int | None = None,
) -> list[dict]:
    """Return active (non-revoked, non-expired) sessions for a user.

    Args:
        user_id: ID of the authenticated user.
        tenant_slug: Tenant schema to query.
        current_session_id: When provided, the matching session gets is_current=True.

    Returns:
        List of dicts compatible with SessionOut schema.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
                "user_agent": r.user_agent,
                "ip_address": r.ip_address,
                "is_current": r.id == current_session_id,
            }
            for r in rows
        ]


async def revoke_session(
    session_id: int,
    user_id: int,
    tenant_slug: str,
    redis=None,
) -> None:
    """Revoke a specific refresh token session (ownership-checked).

    Args:
        session_id: ID of the RefreshToken row to revoke.
        user_id: ID of the authenticated user (ownership check).
        tenant_slug: Tenant schema to query.
        redis: Unused — reserved for future JTI revocation.

    Raises:
        AppError: NOT_FOUND (404) if session doesn't exist or belongs to another user.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        token_row = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.id == session_id,
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
        if token_row is None:
            raise AppError("NOT_FOUND", "Session not found", 404)
        token_row.revoked_at = now
        await session.commit()


async def revoke_all_sessions(
    user_id: int,
    tenant_slug: str,
    current_session_id: int | None = None,
    revoke_current: bool = False,
    redis=None,
) -> None:
    """Revoke all active sessions for a user, optionally keeping the current one.

    Args:
        user_id: ID of the authenticated user.
        tenant_slug: Tenant schema to query.
        current_session_id: ID of the current refresh token session to preserve
            when revoke_current=False.
        revoke_current: When True, revoke ALL sessions including the current one.
            When False, keep the session identified by current_session_id.
        redis: Unused at service level — JTI revocation is handled in the router.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        stmt = update(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        if not revoke_current and current_session_id is not None:
            stmt = stmt.where(RefreshToken.id != current_session_id)
        await session.execute(stmt.values(revoked_at=now))
        await session.commit()


async def forgot_password(body, arq_pool=None) -> None:
    """Genere un token de reinitialisation et l'envoie par email. Toujours sans erreur.

    Si le tenant ou l'email est inconnu, la fonction retourne silencieusement
    pour ne pas reveler l'existence des comptes (anti-enumeration).

    Args:
        body: Payload ForgotPasswordRequest valide par Pydantic.
        arq_pool: Pool arq optionnel pour l'envoi de l'email de reinitialisation.
    """
    try:
        async with get_public_session() as pub:
            result = await pub.execute(
                text("SELECT id FROM public.tenants WHERE slug = :slug"),
                {"slug": body.tenant_slug},
            )
            if result.scalar_one_or_none() is None:
                return  # Tenant inconnu — reponse silencieuse

        async with get_tenant_session(body.tenant_slug) as session:
            user = await session.scalar(
                select(User).where(User.email == body.email, User.is_active.is_(True))
            )
            if user is None:
                return  # Email inconnu — reponse silencieuse

            # 256 bits d'entropie (32 bytes) — envoye en clair par email, seul le hash bcrypt est stocke.
            token = secrets.token_urlsafe(32)
            user.password_reset_token = get_password_hash(token)
            user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            await session.commit()

        if arq_pool is not None:
            try:
                await arq_pool.enqueue_job(
                    "send_password_reset_email",
                    tenant_slug=body.tenant_slug,
                    user_id=user.id,
                    token=token,
                )
            except Exception:
                pass
    except Exception:
        pass  # Absorbe toutes les erreurs — jamais de leak via timing


async def reset_password(body, redis=None) -> dict:
    """Reinitialise le mot de passe via le token recu par email.

    Args:
        body: Payload ResetPasswordRequest valide par Pydantic.
        redis: Client Redis optionnel pour flaguer le user comme desactive.

    Returns:
        Dict {"message": "Password reset successfully"}.

    Raises:
        AppError: INVALID_TOKEN (400) si le token est invalide, expire ou introuvable.
    """
    async with get_public_session() as pub:
        result = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        if result.scalar_one_or_none() is None:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

    async with get_tenant_session(body.tenant_slug) as session:
        user = await session.scalar(
            select(User).where(User.email == body.email, User.is_active.is_(True))
        )
        if user is None or user.password_reset_token is None:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        now = datetime.now(timezone.utc)
        if user.password_reset_expires_at is None or user.password_reset_expires_at < now:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        if not verify_password(body.token, user.password_reset_token):
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        user.password_hash = get_password_hash(body.new_password)
        user.password_reset_token = None
        user.password_reset_expires_at = None
        user.must_change_password = False

        # Revoque tous les refresh tokens actifs
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()

    # Force re-login via user_disabled flag Redis
    if redis is not None:
        from app.core.auth.token_revocation import flag_user_disabled
        await flag_user_disabled(redis, user.id, body.tenant_slug)

    return {"message": "Password reset successfully"}


async def resend_verification(user_id: int, tenant_slug: str, arq_pool=None) -> None:
    """Genere un nouveau token de verification email et l'envoie par email.

    Args:
        user_id: ID de l'utilisateur demandant le renvoi.
        tenant_slug: Slug du tenant de l'utilisateur.
        arq_pool: Pool arq optionnel pour l'envoi de l'email.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
        AppError: ALREADY_VERIFIED (400) si l'email est deja verifie.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.email_verified_at is not None:
            raise AppError("ALREADY_VERIFIED", "Email is already verified", 400)

        user.email_verification_token = str(uuid.uuid4())
        user.email_verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        await session.commit()

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_verification_email",
                tenant_slug=tenant_slug,
                user_id=user_id,
                token=user.email_verification_token,
            )
        except Exception:
            pass


async def change_password(
    user_id: int,
    tenant_slug: str,
    body,
    current_refresh_token_id: int | None = None,
    redis=None,
) -> dict:
    """Change le mot de passe d'un utilisateur authentifie.

    Si must_change_password est True, le mot de passe actuel n'est pas requis.
    Sinon, current_password doit etre fourni et valide.
    Tous les refresh tokens actifs sont revoques apres le changement.

    Args:
        user_id: ID de l'utilisateur.
        tenant_slug: Slug du tenant.
        body: Payload ChangePasswordRequest valide par Pydantic.
        current_refresh_token_id: ID du refresh token courant (reserve pour Task 9).
        redis: Client Redis optionnel (non utilise actuellement).

    Returns:
        Dict {"message": "Password changed successfully"}.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
        AppError: VALIDATION_ERROR (422) si current_password manquant quand requis.
        AppError: INVALID_CREDENTIALS (401) si le mot de passe actuel est incorrect.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)

        if not user.must_change_password:
            if not body.current_password:
                raise AppError("VALIDATION_ERROR", "current_password is required", 422)
            if not verify_password(body.current_password, user.password_hash):
                raise AppError("INVALID_CREDENTIALS", "Current password is incorrect", 401)

        user.password_hash = get_password_hash(body.new_password)
        user.must_change_password = False

        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
        if current_refresh_token_id is not None:
            stmt = stmt.where(RefreshToken.id != current_refresh_token_id)
        await session.execute(stmt.values(revoked_at=now))
        await session.commit()

    return {"message": "Password changed successfully"}
