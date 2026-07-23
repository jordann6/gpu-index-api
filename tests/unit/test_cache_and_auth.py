from datetime import UTC

import pytest
from fastapi import HTTPException

from app.core import cache
from app.core.cache import read_through
from app.core.config import Settings
from app.core.security import Principal, rate_limit, require_api_key
from app.services.pricing import decode_cursor, encode_cursor
from tests.conftest import FakeRedis


async def test_read_through_misses_then_hits():
    client = FakeRedis()
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        return {"value": 42}

    first, hit1 = await read_through(client, "k", producer)
    second, hit2 = await read_through(client, "k", producer)

    assert first == second == {"value": 42}
    assert hit1 is False and hit2 is True
    assert calls["n"] == 1, "producer should run once, second read comes from cache"


async def test_read_through_degrades_when_redis_fails():
    class BrokenRedis(FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    before = cache.STATS["errors"]

    async def producer():
        return {"ok": True}

    value, hit = await read_through(BrokenRedis(), "k", producer)

    assert value == {"ok": True}, "request must still succeed without Redis"
    assert hit is False
    assert cache.STATS["errors"] > before


async def test_require_api_key_rejects_unknown():
    settings = Settings(api_keys="good-key")
    with pytest.raises(HTTPException) as exc:
        await require_api_key(key="bad-key", settings=settings)
    assert exc.value.status_code == 401


async def test_require_api_key_accepts_known():
    settings = Settings(api_keys="good-key,second-key")
    principal = await require_api_key(key="second-key", settings=settings)
    assert principal.api_key == "second-key"


async def test_bucket_id_does_not_leak_the_key():
    principal = Principal(api_key="super-secret")
    assert "super-secret" not in principal.bucket_id


async def test_rate_limit_trips_after_threshold():
    client = FakeRedis()
    settings = Settings(rate_limit_requests=3, rate_limit_window_seconds=60)
    principal = Principal(api_key="k")

    for _ in range(3):
        await rate_limit(principal=principal, client=client, settings=settings)

    with pytest.raises(HTTPException) as exc:
        await rate_limit(principal=principal, client=client, settings=settings)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"


async def test_rate_limit_fails_open_on_redis_error():
    class BrokenRedis(FakeRedis):
        def pipeline(self):
            raise ConnectionError("redis down")

    principal = Principal(api_key="k")
    result = await rate_limit(principal=principal, client=BrokenRedis(), settings=Settings())
    assert result is principal, "throttling must not take the API down with Redis"


async def test_cursor_roundtrip():
    from datetime import datetime

    ts = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
    decoded_ts, decoded_id = decode_cursor(encode_cursor(ts, 991))
    assert decoded_ts == ts
    assert decoded_id == 991
