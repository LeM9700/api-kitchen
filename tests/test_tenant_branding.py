"""Tests Plan 02 — GET /tenant/branding (public) + PATCH /tenant/branding (admin)."""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_schema_name
from app.main import app


async def _reset_branding(slug: str) -> None:
    schema = tenant_schema_name(slug)
    async with engine.begin() as conn:
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        await conn.execute(
            text(
                "UPDATE tenant_config SET "
                "display_name = NULL, "
                "logo_url = NULL, "
                "primary_color = NULL, "
                "secondary_color = NULL, "
                "font_family = NULL"
            )
        )


@pytest.fixture(scope="module")
async def demo_tenant_account():
    """Create an isolated tenant/admin pair through the public registration flow."""
    unique_slug = uuid.uuid4().hex[:8]
    email = f"branding-{unique_slug}@test.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as setup_client:
        response = await setup_client.post(
            "/api/v1/auth/register",
            json={
                "tenant_slug": unique_slug,
                "tenant_name": "Branding Test",
                "email": email,
                "password": "Valid1!aa",
            },
        )
    assert response.status_code == 201, response.text

    token = response.json()["access_token"]
    try:
        yield {
            "slug": unique_slug,
            "headers": {
                "Authorization": f"Bearer {token}",
                "X-Tenant-Slug": unique_slug,
            },
        }
    finally:
        schema = tenant_schema_name(unique_slug)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(
                text(
                    "DELETE FROM public.tenant_configs "
                    "WHERE tenant_id IN (SELECT id FROM public.tenants WHERE slug = :slug)"
                ),
                {"slug": unique_slug},
            )
            await conn.execute(text("DELETE FROM public.tenants WHERE slug = :slug"), {"slug": unique_slug})


@pytest.fixture(autouse=True)
async def reset_demo_branding(demo_tenant_account):
    await _reset_branding(demo_tenant_account["slug"])
    yield
    await _reset_branding(demo_tenant_account["slug"])


@pytest.fixture
async def demo_tenant_slug(demo_tenant_account) -> str:
    return demo_tenant_account["slug"]


@pytest.fixture
async def authed_client(demo_tenant_account):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=demo_tenant_account["headers"],
    ) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# GET /tenant/branding — endpoint public
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_branding_no_auth_required(client: AsyncClient, demo_tenant_slug: str):
    """L'endpoint est accessible sans token JWT."""
    response = await client.get(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        # Pas de header Authorization
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_branding_returns_expected_fields(client: AsyncClient, demo_tenant_slug: str):
    """La réponse contient exactement les 5 champs branding, pas plus."""
    response = await client.get(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
    )
    assert response.status_code == 200
    data = response.json()

    expected_keys = {"display_name", "logo_url", "primary_color", "secondary_color", "font_family"}
    assert set(data.keys()) == expected_keys

    # [⚠️ PROD] Vérification explicite qu'aucun champ sensible n'est exposé.
    for sensitive in ("stripe_secret", "secret", "password", "token", "id", "is_temporarily_closed"):
        assert sensitive not in data, f"Champ sensible exposé : {sensitive}"


@pytest.mark.asyncio
async def test_get_branding_unknown_tenant(client: AsyncClient):
    """404 si le tenant n'existe pas."""
    response = await client.get(
        "/api/v1/tenant/branding",
        params={"tenant_slug": "tenant-qui-nexiste-pas-xyz"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /tenant/branding — admin uniquement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_branding_requires_auth(client: AsyncClient, demo_tenant_slug: str):
    """PATCH /tenant/branding sans auth retourne 401 ou 403."""
    response = await client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"display_name": "Test"},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_patch_branding_updates_display_name(
    authed_client: AsyncClient,
    demo_tenant_slug: str,
):
    """PATCH met à jour display_name et le retourne dans la réponse."""
    response = await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"display_name": "La Bella Pizza"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "La Bella Pizza"


@pytest.mark.asyncio
async def test_patch_branding_validates_hex_color(authed_client: AsyncClient, demo_tenant_slug: str):
    """Un format de couleur invalide est rejeté avec 422."""
    for invalid_color in ("rouge", "#ZZZ", "FF0000", "#FF000"):
        response = await authed_client.patch(
            "/api/v1/tenant/branding",
            params={"tenant_slug": demo_tenant_slug},
            json={"primary_color": invalid_color},
        )
        assert response.status_code == 422, f"Attendu 422 pour '{invalid_color}'"


@pytest.mark.asyncio
async def test_patch_branding_accepts_valid_hex_color(authed_client: AsyncClient, demo_tenant_slug: str):
    """Un format #RRGGBB valide est accepté."""
    response = await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"primary_color": "#E63946", "secondary_color": "#1A1A2E"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["primary_color"] == "#E63946"
    assert data["secondary_color"] == "#1A1A2E"


@pytest.mark.asyncio
async def test_patch_branding_validates_font_family(authed_client: AsyncClient, demo_tenant_slug: str):
    """Une font non supportée est rejetée avec 422."""
    response = await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"font_family": "comic_sans"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_branding_accepts_valid_fonts(authed_client: AsyncClient, demo_tenant_slug: str):
    """Les fonts supportées sont acceptées."""
    for font in ("inter", "poppins", "playfair_display"):
        response = await authed_client.patch(
            "/api/v1/tenant/branding",
            params={"tenant_slug": demo_tenant_slug},
            json={"font_family": font},
        )
        assert response.status_code == 200, f"Font refusée à tort : {font}"
        assert response.json()["font_family"] == font


@pytest.mark.asyncio
async def test_patch_branding_is_partial(authed_client: AsyncClient, demo_tenant_slug: str):
    """PATCH partiel — seul le champ fourni est modifié, les autres conservent leur valeur."""
    # Setup : on fixe d'abord display_name + primary_color
    await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"display_name": "Chez Marco", "primary_color": "#C0392B"},
    )

    # Patch partiel : on change uniquement logo_url
    response = await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"logo_url": "https://res.cloudinary.com/demo/image/upload/logo.png"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "Chez Marco"   # non touché
    assert data["primary_color"] == "#C0392B"      # non touché
    assert data["logo_url"] == "https://res.cloudinary.com/demo/image/upload/logo.png"


@pytest.mark.asyncio
async def test_get_branding_reflects_patch(
    client: AsyncClient,
    authed_client: AsyncClient,
    demo_tenant_slug: str,
):
    """GET branding après PATCH retourne les nouvelles valeurs."""
    await authed_client.patch(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
        json={"display_name": "Pizzeria Roma", "primary_color": "#2ECC71"},
    )
    response = await client.get(
        "/api/v1/tenant/branding",
        params={"tenant_slug": demo_tenant_slug},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "Pizzeria Roma"
    assert data["primary_color"] == "#2ECC71"
