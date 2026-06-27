async def test_stock_ingredients_requires_staff(client):
    response = await client.get("/api/v1/stock/ingredients")
    assert response.status_code == 401
