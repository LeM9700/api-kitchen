from sqlalchemy import text


async def test_can_query_tenants(public_session):
    result = await public_session.execute(text("SELECT 1"))
    assert result.scalar() == 1
