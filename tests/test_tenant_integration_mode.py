"""Tests pour l'infrastructure du mode d'integration tenant (P8).

Couvre l'enum applicatif IntegrationMode et le DDL public poses par la
migration 0044 (public.tenants.integration_mode, public.pos_connections).
"""

import contextlib

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.tenancy.integration_mode import IntegrationMode
from app.modules.pos import service as pos_service


def test_integration_mode_enum_values():
    assert IntegrationMode.STANDALONE == "standalone"
    assert IntegrationMode.CONNECTED == "connected"
    assert {m.value for m in IntegrationMode} == {"standalone", "connected"}


async def test_new_tenant_defaults_to_standalone(public_session, unique_slug):
    await public_session.execute(
        sa.text("INSERT INTO public.tenants (slug, name) VALUES (:slug, 'Test Tenant')"),
        {"slug": unique_slug},
    )
    await public_session.commit()

    mode = await public_session.scalar(
        sa.text("SELECT integration_mode FROM public.tenants WHERE slug = :slug"),
        {"slug": unique_slug},
    )
    assert mode == IntegrationMode.STANDALONE.value


async def test_pos_connections_status_check_constraint(public_session, unique_slug):
    tenant_id = await public_session.scalar(
        sa.text(
            "INSERT INTO public.tenants (slug, name) VALUES (:slug, 'Test Tenant') RETURNING id"
        ),
        {"slug": unique_slug},
    )
    await public_session.commit()

    with pytest.raises(IntegrityError):
        await public_session.execute(
            sa.text(
                "INSERT INTO public.pos_connections "
                "(tenant_id, provider, external_establishment_id, status) "
                "VALUES (:tenant_id, 'lightspeed', 'store-1', 'bogus')"
            ),
            {"tenant_id": tenant_id},
        )


async def test_pos_connections_unique_provider_establishment(public_session, unique_slug):
    tenant_id = await public_session.scalar(
        sa.text(
            "INSERT INTO public.tenants (slug, name) VALUES (:slug, 'Test Tenant') RETURNING id"
        ),
        {"slug": unique_slug},
    )
    await public_session.commit()

    await public_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(tenant_id, provider, external_establishment_id, status) "
            "VALUES (:tenant_id, 'lightspeed', 'store-1', 'pending')"
        ),
        {"tenant_id": tenant_id},
    )
    await public_session.commit()

    with pytest.raises(IntegrityError):
        await public_session.execute(
            sa.text(
                "INSERT INTO public.pos_connections "
                "(tenant_id, provider, external_establishment_id, status) "
                "VALUES (:tenant_id, 'lightspeed', 'store-1', 'active')"
            ),
            {"tenant_id": tenant_id},
        )


async def test_pos_connections_has_refresh_token_and_expiry_columns(public_session, unique_slug):
    tenant_id = await public_session.scalar(
        sa.text(
            "INSERT INTO public.tenants (slug, name) VALUES (:slug, 'Test Tenant') RETURNING id"
        ),
        {"slug": unique_slug},
    )
    await public_session.commit()

    await public_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(tenant_id, provider, external_establishment_id, status) "
            "VALUES (:tenant_id, 'generic_hub', 'store-1', 'pending')"
        ),
        {"tenant_id": tenant_id},
    )
    await public_session.commit()

    row = (
        await public_session.execute(
            sa.text(
                "SELECT refresh_token_encrypted, token_expires_at "
                "FROM public.pos_connections WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
    ).one()
    assert row.refresh_token_encrypted is None
    assert row.token_expires_at is None


async def test_pos_connection_full_round_trip_against_real_db(public_session, unique_slug, monkeypatch):
    """Round-trip reel (pas de fake session) : save_connection -> get_active_connection
    -> disconnect, pour attraper une erreur de colonne/type que les tests unitaires
    (fake session, assertions sur des sous-chaines SQL dans test_pos_connect_service.py)
    ne peuvent pas voir."""
    monkeypatch.setattr(settings, "pos_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pos_hub_provider_name", "generic_hub")

    @contextlib.asynccontextmanager
    async def fake_get_public_session():
        yield public_session

    monkeypatch.setattr(pos_service, "get_public_session", fake_get_public_session)

    await public_session.execute(
        sa.text("INSERT INTO public.tenants (slug, name) VALUES (:slug, 'Test Tenant')"),
        {"slug": unique_slug},
    )
    await public_session.commit()

    token_data = {
        "access_token": "plain-access-token",
        "refresh_token": "plain-refresh-token",
        "expires_in": 3600,
        "external_establishment_id": f"store-{unique_slug}",
        "scope": "orders.read",
    }
    await pos_service.save_connection(unique_slug, token_data)

    connection = await pos_service.get_active_connection(unique_slug)
    assert connection is not None
    assert connection["access_token_encrypted"] != "plain-access-token"

    await pos_service.disconnect(unique_slug)

    assert await pos_service.get_active_connection(unique_slug) is None
