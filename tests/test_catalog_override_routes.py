"""Tests Task 8 : routes PUT/DELETE /catalog/products/{product_id}/override.

Pas de fixture ``authed_admin_client`` (ni sous ce nom ni sous un autre) dans
``tests/conftest.py`` ou les autres ``tests/test_catalog*.py`` -- aucun test
catalog existant n'authentifie encore un client HTTP en admin contre de vraies
lignes DB. Le pattern etabli le plus proche dans le repo pour ce besoin est
celui de ``tests/test_stock_phase3_integration.py`` : ``app.dependency_overrides``
sur ``get_current_user`` + ``monkeypatch`` de ``get_tenant_session`` (tel
qu'importe dans le module router cible) pour rediriger vers la ``db_session``
isolee du test, afin que les lignes creees via ``db_session`` dans les fixtures
soient visibles par les routes appelees via ``client``. Compose ici sous le nom
``authed_admin_client`` attendu par les corps de test du brief.
"""
from contextlib import asynccontextmanager

import pytest


@pytest.fixture
def admin_user_override():
    from app.core.http.deps import get_current_user
    from app.main import app

    current_user = {
        "id": "1",
        "tenant_id": 1,
        "tenant_slug": "pizza_test",
        "role": "admin",
        "email": "admin@pizza-test.test",
    }

    async def _current_user() -> dict:
        return current_user

    app.dependency_overrides[get_current_user] = _current_user
    try:
        yield current_user
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def tenant_session_override(monkeypatch, db_session):
    import sqlalchemy as sa

    # Le search_path est positionne ici (plutot que de compter sur les fixtures
    # synced_product/standalone_product) car test_put_override_rejects_unknown_product
    # appelle la route sans passer par une fixture produit -- sans ce SET, la
    # session tombe sur le schema par defaut ou "products" n'existe pas.
    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))

    @asynccontextmanager
    async def _tenant_session(_tenant_slug: str):
        yield db_session

    monkeypatch.setattr("app.modules.catalog.router.get_tenant_session", _tenant_session)
    return db_session


@pytest.fixture
def _arq_pool_none():
    """Neutralise ``app.state.arq_pool`` -- absent car ``client`` ne declenche
    jamais le lifespan de l'app (voir le meme commentaire dans
    tests/test_catalog_provider.py). ``get_arq_pool`` accede directement a
    ``request.app.state.arq_pool`` : sans cet attribut, meme mis a ``None``,
    la resolution de dependance leve un ``AttributeError`` avant d'atteindre
    la route."""
    from app.main import app

    had_arq_pool = hasattr(app.state, "arq_pool")
    previous_arq_pool = getattr(app.state, "arq_pool", None)
    app.state.arq_pool = None
    try:
        yield
    finally:
        if had_arq_pool:
            app.state.arq_pool = previous_arq_pool
        else:
            del app.state.arq_pool


@pytest.fixture
async def authed_admin_client(client, admin_user_override, tenant_session_override, _arq_pool_none):
    yield client


@pytest.fixture
async def synced_product(db_session):
    import sqlalchemy as sa

    from app.modules.catalog.models import Product

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    product = Product(name="Regina", base_price=11.0, external_product_id="ext-route-1")
    db_session.add(product)
    await db_session.commit()
    return product


@pytest.fixture
async def standalone_product(db_session):
    import sqlalchemy as sa

    from app.modules.catalog.models import Product

    await db_session.execute(sa.text('SET search_path TO "tenant_pizza_test", public'))
    product = Product(name="Margherita", base_price=9.0)  # no external_product_id
    db_session.add(product)
    await db_session.commit()
    return product


async def test_put_override_creates_it(client, authed_admin_client, synced_product):
    response = await authed_admin_client.put(
        f"/api/v1/catalog/products/{synced_product.id}/override",
        json={"is_featured": True, "display_order": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == synced_product.id
    assert body["is_featured"] is True


async def test_put_override_rejects_standalone_product(authed_admin_client, standalone_product):
    response = await authed_admin_client.put(
        f"/api/v1/catalog/products/{standalone_product.id}/override",
        json={"is_featured": True},
    )
    assert response.status_code == 422


async def test_put_override_rejects_unknown_product(authed_admin_client):
    response = await authed_admin_client.put(
        "/api/v1/catalog/products/99999999/override",
        json={"is_featured": True},
    )
    assert response.status_code == 404


async def test_put_override_rejects_price_field(authed_admin_client, synced_product):
    response = await authed_admin_client.put(
        f"/api/v1/catalog/products/{synced_product.id}/override",
        json={"base_price": 99.0},
    )
    assert response.status_code == 422


async def test_delete_override_removes_it(authed_admin_client, synced_product):
    await authed_admin_client.put(
        f"/api/v1/catalog/products/{synced_product.id}/override", json={"is_featured": True}
    )
    response = await authed_admin_client.delete(f"/api/v1/catalog/products/{synced_product.id}/override")
    assert response.status_code == 204


async def test_delete_override_404_when_absent(authed_admin_client, synced_product):
    response = await authed_admin_client.delete(f"/api/v1/catalog/products/{synced_product.id}/override")
    assert response.status_code == 404
