"""Émission et validation des tokens d'impersonation Super Admin.

[SECURITE] Corrige l'incohérence historique entre le flux d'impersonation et
la validation centrale des JWT tenant : l'ancien token portait ``sub="0"`` et
un ``tenant_slug`` non-null, ce qui le faisait tomber dans la branche
``user_belongs_to_tenant(0, tenant_slug, email)`` de
``app.core.http.deps.get_current_user`` — un id 0 n'existe jamais dans un
schéma tenant (les PK démarrent à 1), donc CE FLUX ÉTAIT REJETÉ SYSTÉMATIQUEMENT
(aucun test ne couvrait le succès d'un appel réellement authentifié avec ce
token). Ce module donne à l'impersonation son propre chemin de validation,
qui ne touche jamais ``user_belongs_to_tenant``.

``sub`` porte désormais l'id RÉEL du super-admin acteur (traçable), jamais un
sentinel. Le token est validé par ``validate_impersonation_token`` :
1. le super-admin émetteur existe, est actif, et son ``auth_version`` courant
   correspond au claim du token (une révocation globale de l'émetteur invalide
   donc aussi toute impersonation qu'il a ouverte) ;
2. sa session source (``source_sid``, capturée au moment de l'émission) est
   toujours active, ni révoquée ni expirée — "session source révoquée :
   impersonation refusée" ;
3. l'enregistrement PERSISTANT ``public.super_admin_impersonation_sessions``
   (claim ``impersonation_id``) existe, n'est pas expiré et n'a pas de
   ``revoked_at`` — SOURCE DE VÉRITÉ de la révocation (voir plus bas) ;
4. le tenant ciblé existe toujours, et correspond au tenant épinglé dans
   l'enregistrement persistant (défense en profondeur en plus de la
   signature JWT).

[🔒 SÉCURITÉ] Redis n'est PLUS la seule preuve de révocation. Avant ce
correctif, seule la deny-list Redis (``jti``, voir
``app.core.auth.token_revocation``) rendait un token révoqué -- si Redis
était absent/indisponible, ``/impersonation/end`` pouvait écrire un audit
"ended" alors que le token restait, en réalité, valide jusqu'à expiration.
``public.super_admin_impersonation_sessions.revoked_at`` est désormais la
source de vérité persistante, vérifiée ici indépendamment de Redis ; la
deny-list Redis reste un accélérateur de révocation immédiate (défense en
profondeur), jamais la seule preuve qu'une impersonation est active.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.auth.security import create_access_token, decode_token
from app.core.config import settings
from app.core.database import get_public_session

# [SECURITE] Liste explicite et minimale -- jamais "*", jamais un droit
# d'écriture. Un token d'impersonation sert au support/diagnostic, pas à
# modifier les données du tenant ciblé. Voir has_permission() dans
# app.core.http.deps : pour un token d'impersonation (is_impersonation=True),
# le raccourci "role admin/super-admin => tout autorisé" est désactivé, donc
# cette liste est effectivement appliquée par toute route gated via
# require_permission(). Les routes encore gated par le seul require_role(...)
# (sans permission fine) restent, elles, à pleine capacité du rôle "admin"
# pendant l'impersonation -- limite documentée, hors scope d'un correctif qui
# ne doit pas réécrire toutes les routes admin existantes.
IMPERSONATION_PERMISSIONS = [
    "orders:read",
    "stock:read",
    "catalog:read",
    "payments:read",
    "haccp:read",
    "print:read",
]


def create_impersonation_token(
    actor: dict, tenant_id: int, tenant_slug: str, impersonation_id: str
) -> tuple[str, str, datetime, int]:
    """Émet un token d'impersonation court, non renouvelable, tenant-pinné.

    N'insère JAMAIS l'enregistrement persistant lui-même -- l'appelant (le
    router) doit l'avoir déjà créé (ou le créer dans la même transaction que
    l'audit) et fournir son id ici, pour que le claim ``impersonation_id`` du
    token corresponde toujours à une ligne réelle.

    Args:
        actor: Dict utilisateur du super-admin appelant (voir
            ``get_current_user`` — doit porter ``id``, ``email``, ``sid``,
            ``auth_version``, tous requis pour une session super-admin complète).
        tenant_id: Id public.tenants du tenant ciblé.
        tenant_slug: Slug du tenant ciblé.
        impersonation_id: Id de la ligne ``super_admin_impersonation_sessions``
            associée à ce token (voir docstring module).

    Returns:
        Tuple ``(access_token, jti, expires_at, expires_in_seconds)`` --
        ``expires_at`` est extrait du claim ``exp`` réellement signé, pour que
        l'enregistrement persistant et le JWT n'aient jamais de dérive.
    """
    ttl = timedelta(minutes=settings.super_admin_impersonation_token_minutes)
    payload = {
        "sub": str(actor["id"]),
        "email": f"impersonation:{actor['email']}->{tenant_slug}",
        "role": "admin",
        "tenant_slug": tenant_slug,
        "tenant_id": tenant_id,
        "impersonation": True,
        "impersonation_id": impersonation_id,
        "impersonated_by_super_admin_id": actor["id"],
        "impersonated_by_email": actor["email"],
        "source_sid": actor.get("sid"),
        "auth_version": actor.get("auth_version"),
        "permissions": IMPERSONATION_PERMISSIONS,
    }
    token = create_access_token(payload, expires_delta=ttl)
    decoded = decode_token(token)
    expires_at = datetime.fromtimestamp(decoded["exp"], tz=timezone.utc)
    return token, decoded["jti"], expires_at, int(ttl.total_seconds())


async def validate_impersonation_token(payload: dict) -> bool:
    """Revalide un token d'impersonation à chaque requête.

    Args:
        payload: Claims JWT décodés (``impersonation`` déjà vérifié True par
            l'appelant avant d'invoquer cette fonction).

    Returns:
        True si le token est toujours valide.
    """
    actor_id = payload.get("impersonated_by_super_admin_id")
    actor_email = payload.get("impersonated_by_email")
    auth_version = payload.get("auth_version")
    source_sid = payload.get("source_sid")
    tenant_slug = payload.get("tenant_slug")
    impersonation_id = payload.get("impersonation_id")

    if (
        not actor_id
        or not actor_email
        or auth_version is None
        or not source_sid
        or not tenant_slug
        or not impersonation_id
    ):
        return False

    async with get_public_session() as session:
        admin_result = await session.execute(
            text(
                "SELECT auth_version FROM public.super_admins "
                "WHERE id = :id AND email = :email AND is_active = true"
            ),
            {"id": actor_id, "email": actor_email},
        )
        row = admin_result.first()
        if row is None or row[0] != auth_version:
            return False

        session_result = await session.execute(
            text(
                "SELECT 1 FROM public.super_admin_sessions "
                "WHERE id = :sid AND super_admin_id = :admin_id "
                "AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"sid": source_sid, "admin_id": actor_id},
        )
        if session_result.scalar_one_or_none() is None:
            return False

        # [SECURITE] SOURCE DE VÉRITÉ persistante -- indépendante de Redis.
        # Le tenant_slug est revérifié contre l'enregistrement DB (pas
        # seulement le claim JWT) : défense en profondeur supplémentaire,
        # même si une falsification du claim seul romprait déjà la signature.
        impersonation_result = await session.execute(
            text(
                "SELECT 1 FROM public.super_admin_impersonation_sessions "
                "WHERE id = :id AND super_admin_id = :admin_id AND source_sid = :sid "
                "AND tenant_slug = :tenant_slug "
                "AND revoked_at IS NULL AND expires_at > now()"
            ),
            {
                "id": impersonation_id,
                "admin_id": actor_id,
                "sid": source_sid,
                "tenant_slug": tenant_slug,
            },
        )
        if impersonation_result.scalar_one_or_none() is None:
            return False

        tenant_result = await session.execute(
            text("SELECT 1 FROM public.tenants WHERE slug = :slug"),
            {"slug": tenant_slug},
        )
        return tenant_result.scalar_one_or_none() is not None
