async def test_delivery_check_requires_auth(client):
    response = await client.post("/api/v1/delivery/check", json={"lat": 48.8566, "lng": 2.3522})
    assert response.status_code == 401
