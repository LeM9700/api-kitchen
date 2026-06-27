async def test_stats_daily_requires_admin(client):
    response = await client.get("/api/v1/admin/stats/daily")
    assert response.status_code == 401
