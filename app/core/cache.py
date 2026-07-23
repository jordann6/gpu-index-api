import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis

from app.core.config import get_settings

_client: redis.Redis | None = None

# Observable counters so the cache hit ratio is a measured number rather than
# an assumption. Exposed on /metrics.
STATS = {"hits": 0, "misses": 0, "errors": 0}


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(str(settings.redis_url), encoding="utf-8", decode_responses=True)
    return _client


async def cache_dependency() -> redis.Redis:
    return get_client()


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def read_through(
    client: redis.Redis,
    key: str,
    producer: Callable[[], Awaitable[Any]],
    ttl: int | None = None,
) -> tuple[Any, bool]:
    """Return (value, cache_hit).

    A Redis outage degrades to a direct read rather than failing the request.
    Availability of the API matters more than the cache.
    """
    ttl = ttl if ttl is not None else get_settings().cache_ttl_seconds
    try:
        cached = await client.get(key)
        if cached is not None:
            STATS["hits"] += 1
            return json.loads(cached), True
    except Exception:
        STATS["errors"] += 1

    STATS["misses"] += 1
    value = await producer()

    try:
        await client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception:
        STATS["errors"] += 1

    return value, False


async def invalidate_prefix(client: redis.Redis, prefix: str) -> int:
    """Drop cached entries after an ingest. SCAN avoids blocking on KEYS."""
    removed = 0
    try:
        async for key in client.scan_iter(match=f"{prefix}*", count=500):
            await client.delete(key)
            removed += 1
    except Exception:
        STATS["errors"] += 1
    return removed
