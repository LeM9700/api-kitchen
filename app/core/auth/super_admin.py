"""Validation centrale d'un JWT super-admin plateforme.

[SECURITE] Defense en profondeur -- meme principe que
``app.core.tenancy.tenant.user_belongs_to_tenant``, pour l'autre flux
d'authentification de l'app : le login super-admin independant
(``app/modules/super_admin/router.py``), qui emet des JWT sans ``tenant_slug``.

Un JWT valide (signature correcte) ne suffit plus a lui seul : ce module
revalide, a CHAQUE requete, que :
1. ``sub``/``email`` correspondent a une ligne reelle et active de
   ``public.super_admins`` (sinon un token mint par erreur, ou dont le compte
   a ete desactive depuis, resterait accepte jusqu'a expiration) ;
2. le claim ``auth_version`` du token correspond a la valeur courante en base
   -- une revocation globale (``revoke_all_sessions``) invalide ainsi TOUS les
   tokens deja emis, sans dependre de la deny-list Redis par ``jti`` ;
3. pour un access token de session complete (``role="super-admin"``), le
   ``sid`` reference une ligne ``public.super_admin_sessions`` active
   (ni revoquee, ni expiree) -- une session peut donc etre revoquee
   individuellement sans attendre l'expiration du token.

Le role ``"super-admin-enrollment"`` (token delivre par
``super_admin_login`` a un compte actif qui n'a pas encore active son MFA,
voir ``app/modules/super_admin/service.py``) saute le controle de session : ce
token n'ouvre aucune session et ne sert qu'a appeler ``/mfa/setup``/``/mfa/confirm``.
"""

from sqlalchemy import text

from app.core.database import get_public_session


async def validate_super_admin_session(
    admin_id: int,
    email: str | None,
    auth_version: int | None,
    sid: str | None,
    *,
    require_session: bool,
) -> bool:
    """Verifie qu'un JWT super-admin est toujours valide.

    Args:
        admin_id: ``sub`` du JWT (id dans ``public.super_admins``).
        email: Claim ``email`` du JWT.
        auth_version: Claim ``auth_version`` du JWT (absent = toujours invalide,
            un token emis par le flux actuel porte systematiquement ce claim).
        sid: Claim ``sid`` du JWT (session complete uniquement).
        require_session: True pour un access token ``role="super-admin"``
            (session complete, doit reference une ligne active de
            ``super_admin_sessions``) ; False pour
            ``role="super-admin-enrollment"`` (aucune session, portee limitee
            a l'enrolement MFA).

    Returns:
        True si le token est toujours valide, False sinon.
    """
    if email is None or auth_version is None:
        return False

    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT auth_version FROM public.super_admins "
                "WHERE id = :id AND email = :email AND is_active = true"
            ),
            {"id": admin_id, "email": email},
        )
        row = result.first()
        if row is None or row[0] != auth_version:
            return False

        if not require_session:
            return True

        if not sid:
            return False

        session_result = await session.execute(
            text(
                "SELECT 1 FROM public.super_admin_sessions "
                "WHERE id = :sid AND super_admin_id = :admin_id "
                "AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"sid": sid, "admin_id": admin_id},
        )
        return session_result.scalar_one_or_none() is not None
