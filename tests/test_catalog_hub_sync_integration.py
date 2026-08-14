"""Integration test proving the full hub catalog sync chain actually connects:

    webhook-shaped trigger -> sync_catalog_from_hub (worker task) -> normalize ->
    catalog_snapshots (persisted) -> HubCatalogProvider.get_catalog -> served
    ProductSummaryOut.

Every other test in this plan mocks the adjacent layer only: webhook tests
(tests/test_pos_catalog_webhook.py) mock resolve_connection_id/redis and never touch
the worker task; provider tests (tests/test_hub_catalog_provider.py) seed
catalog_snapshots directly via snapshot_repository, never going through the sync
task; worker task tests (tests/test_worker_catalog_sync.py) mock the HTTP client and
only assert on the snapshot row, never read it back through the provider. None of
them proves the pieces actually wire together end to end. This is the one test that
does -- deliberately scoped to a single realistic scenario, not a new test suite.

Calls sync_catalog_from_hub directly (not via the webhook HTTP layer -- the HTTP
layer's signature verification / connection resolution is already covered by
tests/test_pos_catalog_webhook.py; this test's job is the WORKER -> SNAPSHOT ->
PROVIDER chain, which nothing else proves).
"""
from unittest.mock import AsyncMock

import sqlalchemy as sa


async def _seed_active_connection(db_session, connection_id: int, tenant_slug: str = "pizza_test") -> None:
    """Same seeding convention as tests/test_worker_catalog_sync.py::_seed_active_connection."""
    await db_session.execute(sa.text("SET search_path TO public"))
    tenant_id = await db_session.scalar(
        sa.text("SELECT id FROM public.tenants WHERE slug = :slug"), {"slug": tenant_slug}
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO public.pos_connections "
            "(id, tenant_id, provider, external_establishment_id, access_token_encrypted, status, connected_at) "
            "VALUES (:id, :tenant_id, 'generic_hub', :external_establishment_id, 'cipher', 'active', now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": connection_id, "tenant_id": tenant_id, "external_establishment_id": f"est-{connection_id}"},
    )
    await db_session.commit()


async def test_hub_sync_chain_connects_worker_snapshot_and_provider(db_session, monkeypatch):
    """Would fail if HubCatalogProvider read from the wrong table/column, or if
    normalize_catalog dropped/mismapped a field (price, tax_rate, is_active) --
    it asserts on the actual values the fake hub sent, not synthetic unit fixtures,
    and re-confirms an is_active=False hub product stays excluded under a real
    end-to-end flow (not just the synthetic data used by the provider's own tests)."""
    from app.core.config import settings
    from app.core.http.schemas import PaginationParams
    from app.modules.catalog import snapshot_repository
    from app.modules.catalog.providers import HubCatalogProvider
    from worker.tasks import catalog_sync

    connection_id = 90301
    await _seed_active_connection(db_session, connection_id=connection_id)

    # Redirect the task's own engine/session creation to the test's isolated
    # db_session -- same wiring proof as
    # tests/test_worker_catalog_sync.py::_patch_engine_and_sessions.
    fake_engine = AsyncMock()
    monkeypatch.setattr(catalog_sync, "create_async_engine", lambda *a, **kw: fake_engine)
    monkeypatch.setattr(catalog_sync, "async_sessionmaker", lambda *a, **kw: (lambda: db_session))
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))

    fake_hub_payload = {
        "products": [
            {"id": "ext-1", "name": "Regina", "price": 11.5, "tax_rate": 0.1, "is_active": True},
            {"id": "ext-2", "name": "Margherita", "price": 8.9, "tax_rate": 0.055, "is_active": True},
            {"id": "ext-3", "name": "Discontinued Special", "price": 99.0, "tax_rate": 0.2, "is_active": False},
        ]
    }
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value=fake_hub_payload),
    )

    # 1-3. Fake the hub call and run the real worker task (not via the HTTP webhook layer).
    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=connection_id)

    # 4. A catalog_snapshots row exists with the expected normalized content.
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    snapshot = await snapshot_repository.get_snapshot(db_session, connection_id=connection_id)
    assert snapshot is not None
    assert {item["external_id"] for item in snapshot.normalized} == {"ext-1", "ext-2", "ext-3"}

    # 5. HubCatalogProvider, against the SAME tenant session, serves what the fake hub sent.
    provider = HubCatalogProvider(connection_id=connection_id)
    summaries, total = await provider.get_catalog(db_session, PaginationParams(page=1, page_size=10))

    served_names = {s.name for s in summaries}
    assert served_names == {"Regina", "Margherita"}  # ext-3 (is_active=False) excluded
    assert total == 2

    regina = next(s for s in summaries if s.name == "Regina")
    assert regina.base_price == 11.5
    assert regina.tax_rate == 0.1

    margherita = next(s for s in summaries if s.name == "Margherita")
    assert margherita.base_price == 8.9
    assert margherita.tax_rate == 0.055


async def test_hub_synced_product_can_be_ordered_via_real_product_id(db_session, monkeypatch):
    """Task 9 proof (2026-08-12-hub-catalog-materialization plan): a hub-synced
    product's real `products.id` -- not a surrogate CRC32 id -- is resolvable by
    app.modules.orders.service.create_order.

    This is the concrete end-to-end fix for the divergence the prior lot's
    (2026-08-11-hub-catalog-sync) final review flagged: order creation reads the
    real `products` table directly, so a hub-synced product had to become a real
    row (materialized by sync_catalog_from_hub, Tasks 1-8 of this plan) before it
    could actually be ordered."""
    from app.core.config import settings
    from app.modules.catalog.models import Product
    from app.modules.orders import service as orders_service
    from app.modules.orders.schemas import OrderCreate, OrderItemCreate
    from worker.tasks import catalog_sync

    connection_id = 90302
    await _seed_active_connection(db_session, connection_id=connection_id)

    fake_engine = AsyncMock()
    monkeypatch.setattr(catalog_sync, "create_async_engine", lambda *a, **kw: fake_engine)
    monkeypatch.setattr(catalog_sync, "async_sessionmaker", lambda *a, **kw: (lambda: db_session))
    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(catalog_sync, "acquire_sync_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(catalog_sync, "release_sync_lock", AsyncMock())
    monkeypatch.setattr(catalog_sync, "check_rate_limit", AsyncMock(return_value=True))

    fake_hub_payload = {
        "products": [
            {"id": "ext-order-1", "name": "Quattro Stagioni", "price": 12.9, "tax_rate": 0.1, "is_active": True},
        ]
    }
    monkeypatch.setattr(
        catalog_sync.HttpHubCatalogClient,
        "fetch_catalog",
        AsyncMock(return_value=fake_hub_payload),
    )

    # 1-3. Fake the hub call and run the real worker task, exactly as above.
    await catalog_sync.sync_catalog_from_hub({"redis": AsyncMock()}, connection_id=connection_id)

    # 4. Materialization (Tasks 1-8): the synced product is a real `products` row
    # keyed on external_product_id, not a surrogate id -- fetch its real primary
    # key the same way order creation / product detail routes would.
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    product = await db_session.scalar(
        sa.select(Product).where(Product.external_product_id == "ext-order-1")
    )
    assert product is not None
    assert float(product.base_price) == 12.9  # sanity: same value the fake hub sent

    # 5. Order creation against the synced product's real id succeeds (not
    # PRODUCT_NOT_FOUND) and resolves the price from the catalog server-side,
    # matching the synced product's base_price.
    body = OrderCreate(
        order_type="pickup",
        items=[OrderItemCreate(product_id=product.id, quantity=1)],
    )
    order = await orders_service.create_order(
        db_session, body, idempotency_key="hub-sync-order-test-90302"
    )

    assert order.id is not None
    assert float(order.subtotal) == float(product.base_price)
    assert float(order.total) == float(product.base_price)
