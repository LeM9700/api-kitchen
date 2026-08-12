class _StatefulFakeRedis:
    """Double minimal avec un vrai INCR/EXPIRE/SET NX/DELETE (dict en memoire),
    suivant le meme pattern que _StatefulFakeRedis dans test_pos_connect_service.py."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def incr(self, key):
        self._store[key] = str(int(self._store.get(key, "0")) + 1)
        return int(self._store[key])

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)


async def test_check_rate_limit_allows_up_to_the_limit():
    from app.modules.catalog.sync_guards import check_rate_limit

    redis = _StatefulFakeRedis()
    results = [await check_rate_limit(redis, connection_id=1, limit_per_minute=3) for _ in range(3)]
    assert results == [True, True, True]


async def test_check_rate_limit_blocks_beyond_the_limit():
    from app.modules.catalog.sync_guards import check_rate_limit

    redis = _StatefulFakeRedis()
    for _ in range(3):
        await check_rate_limit(redis, connection_id=1, limit_per_minute=3)
    assert await check_rate_limit(redis, connection_id=1, limit_per_minute=3) is False


async def test_check_rate_limit_is_scoped_per_connection():
    from app.modules.catalog.sync_guards import check_rate_limit

    redis = _StatefulFakeRedis()
    for _ in range(3):
        await check_rate_limit(redis, connection_id=1, limit_per_minute=3)
    assert await check_rate_limit(redis, connection_id=2, limit_per_minute=3) is True


async def test_acquire_sync_lock_blocks_second_caller():
    from app.modules.catalog.sync_guards import acquire_sync_lock

    redis = _StatefulFakeRedis()
    assert await acquire_sync_lock(redis, connection_id=1, ttl_seconds=60) is True
    assert await acquire_sync_lock(redis, connection_id=1, ttl_seconds=60) is False


async def test_release_sync_lock_allows_reacquisition():
    from app.modules.catalog.sync_guards import acquire_sync_lock, release_sync_lock

    redis = _StatefulFakeRedis()
    await acquire_sync_lock(redis, connection_id=1, ttl_seconds=60)
    await release_sync_lock(redis, connection_id=1)
    assert await acquire_sync_lock(redis, connection_id=1, ttl_seconds=60) is True
