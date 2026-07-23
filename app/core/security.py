import time
from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.cache import cache_dependency
from app.core.config import Settings, get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    api_key: str

    @property
    def bucket_id(self) -> str:
        # Never key a bucket on the raw secret.
        return f"rl:{hash(self.api_key) & 0xFFFFFFFF:08x}"


async def require_api_key(
    key: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not key or key not in settings.api_key_set:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
        )
    return Principal(api_key=key)


async def rate_limit(
    principal: Principal = Depends(require_api_key),
    client: redis.Redis = Depends(cache_dependency),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Fixed-window counter in Redis, incremented atomically via pipeline.

    Fails open on a Redis outage: throttling is less important than serving.
    """
    window = settings.rate_limit_window_seconds
    bucket = f"{principal.bucket_id}:{int(time.time()) // window}"

    try:
        pipe = client.pipeline()
        pipe.incr(bucket, 1)
        pipe.expire(bucket, window * 2)
        count, _ = await pipe.execute()
    except Exception:
        return principal

    if int(count) > settings.rate_limit_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit of {settings.rate_limit_requests} per {window}s exceeded.",
            headers={"Retry-After": str(window)},
        )
    return principal
