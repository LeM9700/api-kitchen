"""Tests -- defense en profondeur sur le flux d'auth super-admin independant.

Contexte : app/modules/super_admin/router.py::super_admin_login emet des JWT
"role": "super-admin", "tenant_slug": None, "sid": ..., "auth_version": ...
(voir app/modules/super_admin/service.py). get_current_user()
(app/core/http/deps.py) ne fait plus confiance au seul claim "role" : il
revalide, a CHAQUE requete, que le sub/email correspondent a une ligne reelle
et active de public.super_admins, que auth_version est a jour (revocation
globale) et que le sid reference une session public.super_admin_sessions
active (revocation individuelle) -- voir
app.core.auth.super_admin.validate_super_admin_session.

Meme famille de gap que tests/test_jwt_tenant_mismatch.py (P0), pour l'autre
chemin d'authentification de l'app. Utilise un vrai compte public.super_admins
et une vraie session public.super_admin_sessions inseres directement (pas de
login complet requis : create_access_token() est la vraie fonction de
signature -- seul le contenu du payload varie selon le test) et un endpoint
reel gate par require_role(..., "super-admin") (GET /payments/connect/status,
deja utilise par tests/test_payments_connect.py).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy import text

from app.core.auth.security import compute_token_lookup, create_access_token, create_refresh_token
from app.core.database import get_public_session


async def _create_super_admin(email: str, is_active: bool = True, auth_version: int = 1) -> int:
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.super_admins (email, password_hash, is_active, auth_version) "
                "VALUES (:email, 'unused-hash', :is_active, :auth_version) RETURNING id"
            ),
            {"email": email, "is_active": is_active, "auth_version": auth_version},
        )
        admin_id = result.scalar_one()
        await session.commit()
        return admin_id


async def _create_session(admin_id: int, revoked: bool = False) -> str:
    """Insere une ligne public.super_admin_sessions active (ou revoquee)."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with get_public_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.super_admin_sessions "
                "(id, super_admin_id, refresh_token_lookup, expires_at, revoked_at) "
                "VALUES (:sid, :admin_id, :lookup, :expires_at, :revoked_at)"
            ),
            {
                "sid": sid,
                "admin_id": admin_id,
                "lookup": compute_token_lookup(f"unused-{sid}"),
                "expires_at": now + timedelta(days=7),
                "revoked_at": now if revoked else None,
            },
        )
        await session.commit()
    return sid


def _super_admin_token(
    admin_id: int,
    email: str,
    sid: str | None = None,
    auth_version: int = 1,
) -> str:
    """Mint un token conforme au flux reel (voir service.login) -- role,
    sid et auth_version presents des lors qu'une session complete existe."""
    return create_access_token({
        "sub": str(admin_id),
        "email": email,
        "role": "super-admin",
        "tenant_slug": None,
        "tenant_id": None,
        "sid": sid,
        "auth_version": auth_version,
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
    sid = await _create_session(admin_id)
    token = _super_admin_token(admin_id, email, sid=sid)

    resp = await _call_gated_route(token)

    assert resp.status_code == 200, resp.text


async def test_forged_super_admin_token_without_real_account_is_rejected(unique_slug):
    """[SECURITE] Le coeur du correctif : role=super-admin seul ne suffit plus --
    aucune ligne public.super_admins ne correspond a ce sub/email."""
    token = _super_admin_token(
        999_999_999, f"nobody-{unique_slug}@platform.test", sid=str(uuid.uuid4())
    )

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_rejected_after_account_deactivated(unique_slug):
    """[SECURITE] Un token mint avant la desactivation du compte ne doit plus
    etre accepte -- avant ce correctif, rien ne revalidait is_active apres le
    login (contrairement aux utilisateurs tenant, via is_user_disabled)."""
    email = f"sa-deact-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email, is_active=False)
    sid = await _create_session(admin_id)
    token = _super_admin_token(admin_id, email, sid=sid)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_email_mismatch_is_rejected(unique_slug):
    """[SECURITE] sub reel mais email du claim incoherent avec le compte --
    meme logique que user_belongs_to_tenant pour les tokens tenant."""
    email = f"sa-real-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    sid = await _create_session(admin_id)
    token = _super_admin_token(admin_id, f"attacker-{unique_slug}@evil.test", sid=sid)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_without_sid_is_rejected(unique_slug):
    """[SECURITE] Un access token role=super-admin sans sid n'ouvre plus
    aucune capacite -- une session complete DOIT reference une ligne active
    de super_admin_sessions (l'ancien flux ne mintait ni jti ni session)."""
    email = f"sa-nosid-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    token = _super_admin_token(admin_id, email, sid=None)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_with_revoked_session_is_rejected(unique_slug):
    """[SECURITE] Revocation individuelle -- la session existe mais est revoquee
    (logout, ou revoke-all-sessions)."""
    email = f"sa-revoked-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    sid = await _create_session(admin_id, revoked=True)
    token = _super_admin_token(admin_id, email, sid=sid)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_token_with_stale_auth_version_is_rejected(unique_slug):
    """[SECURITE] Revocation globale -- auth_version du token ne correspond
    plus a la valeur courante en base (revoke_all_sessions l'a incrementee)."""
    email = f"sa-staleav-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email, auth_version=2)
    sid = await _create_session(admin_id)
    token = _super_admin_token(admin_id, email, sid=sid, auth_version=1)

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text


async def test_super_admin_refresh_token_is_never_accepted_as_access(unique_slug):
    """[SECURITE] Un refresh token super-admin (type="refresh") ne doit jamais
    etre accepte comme access token, meme avec un sid/sub valides."""
    email = f"sa-refresh-{unique_slug}@platform.test"
    admin_id = await _create_super_admin(email)
    sid = await _create_session(admin_id)
    token = create_refresh_token({"sub": str(admin_id), "role": "super-admin", "sid": sid})

    resp = await _call_gated_route(token)

    assert resp.status_code == 401, resp.text
