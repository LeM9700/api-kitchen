"""Tests -- defense en profondeur sur le flux d'auth super-admin independant.

Contexte : app/modules/super_admin/router.py::super_admin_login emet des JWT
"role": "super-admin", "tenant_slug": None. Avant ce correctif,
get_current_user() (app/core/http/deps.py) faisait confiance a ce seul claim
"role" -- aucune verification que le "sub"/"email" correspondent a une ligne
reelle et active de public.super_admins. Un token valide (signature correcte)
mais dont le payload est incoherent (bug de mint ailleurs, ou compte
desactive apres emission du token) restait accepte jusqu'a expiration.

Meme famille de gap que tests/test_jwt_tenant_mismatch.py (P0), pour l'autre
chemin d'authentification de l'app. Utilise un vrai compte public.super_admins
insere directement (pas de login complet requis : create_access_token() est
la vraie fonction de signature -- seul le contenu du payload varie selon le
test) et un endpoint reel gate par require_role(..., "super-admin")
(GET /payments/connect/status, deja utilise par tests/test_payments_connect.py).
"""
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from app.core.auth.security import create_access_token
from app.core.database import get_public_session


async def _create_super_admin(email: str, is_active: bool = True) -> int:
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active) "
                "VALUES (:email, 'unused-hash', :is_active) RETURNING id"
            ),
            {"email": email, "is_active": is_active},
        )
        admin_id = result.scalar_one()
        await session.commit()
        return admin_id


def _super_admin_token(admin_id: int, email: str) -> str:
    """Mint un token conforme au flux reel (voir super_admin_login) --
    tenant_slug/tenant_id absents, role=super-admin."""
    return create_access_token({
        "sub": str(admin_id),
        "email": email,
        "role": "super-admin",
        "tenant_slug": None,
        "tenant_id": None,
    })


async def _call_gated_route(token: str):
    """GET /payments/connect/status : route reelle gate par
    require_role("admin", "super-admin"), service mocke pour ne pas
    dependre de Stripe -- seule la couche auth nous interesse ici."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    with patch(
        "app.modules.payments.connect_router.connect_service.get_connect_status",
        new=AsyncMock(return_value={
            "stripe_account_id": None,
            "details_submitted": False,
            "payouts_enabled": False,
            "charges_enabled": False,
            "onboarding_complete": False,
        }),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/api/v1/payments/connect/status",
                params={"tenant_slug": "acme"},
                headers={"Authorization": f"Bearer {token}"},
            )


async def test_real_super_admin_account_is_accepted(unique_slug):
    email = f"sa-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    token = _super_admin_token(admin_id, email)

    resp = await _call_gated_route(token)

    assert resp.status_code == 200, resp.text


async def test_forged_super_admin_token_without_real_account_is_rejected(unique_slug):
    """[SECURITE] Le coeur du correctif : role=super-admin seul ne suffit plus --
    aucune ligne public.super_admins ne correspond a ce sub/email."""
    token = _super_admin_token(999_999_999, f"nobody-{unique_slug}@platform.test")

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_rejected_after_account_deactivated(unique_slug):
    """[SECURITE] Un token mint avant la desactivation du compte ne doit plus
    etre accepte -- avant ce correctif, rien ne revalidait is_active apres le
    login (contrairement aux utilisateurs tenant, via is_user_disabled)."""
    email = f"sa-deact-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email, is_active=False)
    token = _super_admin_token(admin_id, email)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_email_mismatch_is_rejected(unique_slug):
    """[SECURITE] sub reel mais email du claim incoherent avec le compte --
    meme logique que user_belongs_to_tenant pour les tokens tenant."""
    email = f"sa-real-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    token = _super_admin_token(admin_id, f"attacker-{unique_slug}@evil.test")

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text
