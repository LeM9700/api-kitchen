"""Verifie qu'un JWT super-admin est bien backe par un compte reel.

[SECURITE] Defense en profondeur -- meme principe que
``app.core.tenancy.tenant.user_belongs_to_tenant``, pour l'autre flux
d'authentification de l'app : le login super-admin independant
(``app/modules/super_admin/router.py::super_admin_login``), qui emet des
JWT sans ``tenant_slug`` (voir sa docstring). Sans ce controle,
``get_current_user()`` acceptait n'importe quel JWT valide (signature
correcte) portant ``role: "super-admin"`` sans jamais verifier que le
``sub``/``email`` correspondent a une ligne reelle et active de
``public.super_admins`` -- un token mint par erreur, ou dont le compte a
ete desactive depuis, restait accepte jusqu'a expiration.
"""

from sqlalchemy import text

from app.core.database import get_public_session


async def super_admin_exists(admin_id: int, email: str | None) -> bool:
    """Verifie que ``admin_id`` est un super-admin actif dont l'email correspond.

    Args:
        admin_id: ``sub`` du JWT (id dans ``public.super_admins``).
        email: Claim ``email`` du JWT.

    Returns:
        True si un compte actif avec cet id ET cet email existe.
    """
    if email is None:
        return False

    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT 1 FROM public.super_admins "
                "WHERE id = :id AND email = :email AND is_active = true"
            ),
            {"id": admin_id, "email": email},
        )
        return result.scalar_one_or_none() is not None
