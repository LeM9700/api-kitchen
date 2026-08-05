"""Tests pour l'infrastructure du mode d'integration tenant (P8).

Couvre l'enum applicatif IntegrationMode et le DDL public poses par la
migration 0044 (public.tenants.integration_mode, public.pos_connections).
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.core.tenancy.integration_mode import IntegrationMode


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
