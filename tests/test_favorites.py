"""Tests de fumée — module favorites (app/modules/favorites/).

Coverage:
    GET    /api/v1/favorites             — requires auth (401 without token)
    POST   /api/v1/favorites             — creates a favorite, idempotent on repeat
    DELETE /api/v1/favorites/{product_id} — removes a favorite, idempotent if absent
    GET    /api/v1/favorites             — lists the current user's favorites, newest first
    scoping — un favori d'un tenant/user n'est jamais visible d'un autre

Même pattern que tests/test_haccp.py : enregistre un vrai tenant via
`/auth/register` et utilise le token Bearer renvoyé, contre la vraie DB
(``settings.database_url``) — PAS ``bootstrap_default_tenant``/``pizza_test``,
qui n'est visible que des fixtures ``db_session`` (moteur dédié), jamais du
moteur global utilisé par le routeur via ``get_tenant_session``.
"""

FAVORITES_URL = "/api/v1/favorites"


async def _register_admin(client, slug: str) -> dict:
    resp = await client.post("/api/v1/auth/register", json={
        "tenant_slug": slug,
        "tenant_name": f"Pizzeria {slug}",
        "email": f"admin-{slug}@test.com",
        "password": "Valid1!aa",
    })
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


async def test_list_favorites_requires_auth(client):
    response = await client.get(FAVORITES_URL)
    assert response.status_code == 401


async def test_add_list_and_remove_favorite(client, unique_slug):
    admin = await _register_admin(client, f"fav{unique_slug}")
    h = admin["headers"]
    product_id = 777001

    create_response = await client.post(
        FAVORITES_URL, json={"product_id": product_id}, headers=h
    )
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["product_id"] == product_id

    list_response = await client.get(FAVORITES_URL, headers=h)
    assert list_response.status_code == 200
    assert [item["product_id"] for item in list_response.json()] == [product_id]

    delete_response = await client.delete(
        f"{FAVORITES_URL}/{product_id}", headers=h
    )
    assert delete_response.status_code == 204

    list_after_delete = await client.get(FAVORITES_URL, headers=h)
    assert list_after_delete.json() == []


async def test_add_favorite_is_idempotent(client, unique_slug):
    admin = await _register_admin(client, f"favi{unique_slug}")
    h = admin["headers"]
    product_id = 777002

    first = await client.post(FAVORITES_URL, json={"product_id": product_id}, headers=h)
    second = await client.post(FAVORITES_URL, json={"product_id": product_id}, headers=h)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    list_response = await client.get(FAVORITES_URL, headers=h)
    product_ids = [item["product_id"] for item in list_response.json()]
    assert product_ids.count(product_id) == 1


async def test_remove_favorite_is_idempotent_when_absent(client, unique_slug):
    admin = await _register_admin(client, f"favr{unique_slug}")
    h = admin["headers"]

    response = await client.delete(f"{FAVORITES_URL}/999999", headers=h)
    assert response.status_code == 204


async def test_favorites_are_scoped_per_tenant(client, unique_slug):
    tenant_a = await _register_admin(client, f"fava{unique_slug}")
    tenant_b = await _register_admin(client, f"favb{unique_slug}")
    product_id = 777003

    create_response = await client.post(
        FAVORITES_URL, json={"product_id": product_id}, headers=tenant_a["headers"]
    )
    assert create_response.status_code == 201

    list_b = await client.get(FAVORITES_URL, headers=tenant_b["headers"])
    assert list_b.json() == []

    list_a = await client.get(FAVORITES_URL, headers=tenant_a["headers"])
    assert [item["product_id"] for item in list_a.json()] == [product_id]
