"""Modèle SQLAlchemy pour la table public.super_admins.

[🔒 SÉCURITÉ] Cette table est dans le schéma public (cross-tenant).
Elle est complètement séparée du système d'auth tenant.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text as sa_Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PublicBase(DeclarativeBase):
    pass


class SuperAdmin(PublicBase):
    """Super administrateur plateforme — authentification indépendante des tenants.

    Attributes:
        id: Clé primaire auto-incrémentée.
        email: Adresse email unique, utilisée comme identifiant de connexion.
        password_hash: Hash bcrypt du mot de passe.
        is_active: Compte activé ou non.
        created_at: Date de création du compte.
        last_login_at: Dernière connexion réussie (mise à jour au login).
        mfa_secret_encrypted: Secret TOTP chiffré au repos (Fernet, jamais en clair).
        mfa_enabled: MFA actif — tant que False, le login ne délivre qu'un token
            d'enrôlement restreint (voir app.modules.super_admin.router).
        auth_version: Compteur de révocation globale — un login n'est accepté
            (voir app.core.auth.super_admin.validate_super_admin_session) que si
            le claim ``auth_version`` du JWT correspond à cette valeur. L'incrémenter
            invalide instantanément tous les access/refresh tokens déjà émis.
    """

    __tablename__ = "super_admins"
    __table_args__ = {"schema": "public"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(sa_Text, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class SuperAdminRecoveryCode(PublicBase):
    """Code de récupération MFA à usage unique pour un super-admin.

    [🔒 SÉCURITÉ] ``code_hash`` est un hash bcrypt (jamais le code en clair).
    ``used_at`` rend la consommation définitive et auditable.
    """

    __tablename__ = "super_admin_recovery_codes"
    __table_args__ = {"schema": "public"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    super_admin_id: Mapped[int] = mapped_column(
        ForeignKey("public.super_admins.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SuperAdminSession(PublicBase):
    """Session super-admin dédiée — une ligne par refresh token actif.

    ``id`` (le ``sid``) est injecté dans les claims des JWT access et refresh :
    c'est la clé de révocation individuelle (voir app.core.auth.super_admin).

    Attributes:
        id: UUID4 (str) — claim ``sid`` des tokens émis pour cette session.
        refresh_token_lookup: HMAC-SHA256 du refresh token en clair (même
            mécanisme que ``RefreshToken.token_lookup`` côté tenant, voir
            ``compute_token_lookup``) — jamais le token lui-même.
        expires_at: Expiration du refresh token courant ; prolongée à chaque
            rotation réussie.
        revoked_at: Non-null si la session a été révoquée (logout, révocation
            globale, désactivation du compte).
    """

    __tablename__ = "super_admin_sessions"
    __table_args__ = {"schema": "public"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    super_admin_id: Mapped[int] = mapped_column(
        ForeignKey("public.super_admins.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_lookup: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SuperAdminImpersonationSession(PublicBase):
    """Enregistrement persistant d'une impersonation — SOURCE DE VÉRITÉ de sa
    validité, indépendante de Redis.

    [🔒 SÉCURITÉ] Avant cette table, la révocation d'un token d'impersonation
    reposait uniquement sur la deny-list Redis (``jti``) : si Redis était
    absent/indisponible, ``/impersonation/end`` pouvait écrire un audit
    "ended" alors que le token restait en réalité valide jusqu'à expiration.
    Chaque token d'impersonation porte désormais un claim ``impersonation_id``
    référençant une ligne ici (voir ``app.core.auth.impersonation``) : la
    validation vérifie ``revoked_at IS NULL AND expires_at > now()`` sur
    CETTE table à chaque requête. Redis reste un accélérateur de révocation
    immédiate (défense en profondeur), jamais la seule preuve.

    Attributes:
        id: UUID4 (str) — claim ``impersonation_id`` du token émis.
        super_admin_id: Acteur Super Admin à l'origine de l'impersonation.
        source_sid: Session Super Admin ayant émis l'impersonation (voir
            ``SuperAdminSession.id``) — révoquée, elle invalide aussi cette
            impersonation (voir ``validate_impersonation_token``).
        tenant_slug: Tenant cible, strictement épinglé (jamais modifiable
            après émission).
        expires_at: Expiration — identique au claim ``exp`` du JWT.
        revoked_at: Non-null si ``/impersonation/end`` a été appelé.
        reason: Motif de révocation optionnel (ex. "ended_by_user").
    """

    __tablename__ = "super_admin_impersonation_sessions"
    __table_args__ = {"schema": "public"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    super_admin_id: Mapped[int] = mapped_column(
        ForeignKey("public.super_admins.id", ondelete="CASCADE"), nullable=False
    )
    source_sid: Mapped[str] = mapped_column(
        ForeignKey("public.super_admin_sessions.id", ondelete="CASCADE"), nullable=False
    )
    tenant_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlatformAuditLog(PublicBase):
    """Journal d'audit central et persistant des actions Super Admin.

    [🔒 SÉCURITÉ] Table volontairement indépendante de tout schéma tenant — les
    actions auditées ici (connexion, MFA, impersonation, suspension de tenant,
    révocation de session) sont cross-tenant par nature. Voir
    ``app.core.audit.platform_audit`` pour la politique de rédaction des
    secrets et le caractère "fail-closed" de l'écriture pour les actions
    privilégiées.
    """

    __tablename__ = "platform_audit_logs"
    __table_args__ = {"schema": "public"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_super_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("public.super_admins.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True, name="metadata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
