import logging
import os
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from proxy.config import REDIS_URL

logger = logging.getLogger("llmproxy.ratelimit")

RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))


# ---------------------------------------------------------------------------
# In-memory rate limiter (default)
# ---------------------------------------------------------------------------

class MemoryRateLimiter:
    def __init__(self):
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_rate_limited(self, key: str, rpm: int) -> bool:
        now = time.time()
        window_start = now - 60.0
        self._requests[key] = [t for t in self._requests[key] if t > window_start]
        if len(self._requests[key]) >= rpm:
            return True
        self._requests[key].append(now)
        return False


# ---------------------------------------------------------------------------
# Redis rate limiter (production — atomic, shared across workers)
# ---------------------------------------------------------------------------

class RedisRateLimiter:
    def __init__(self, redis_url: str):
        import redis
        self._redis = redis.from_url(redis_url)

    def is_rate_limited(self, key: str, rpm: int) -> bool:
        redis_key = f"llmproxy:rl:{key}"
        pipe = self._redis.pipeline()
        now = time.time()
        window_start = now - 60.0

        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {str(now): now})
        pipe.expire(redis_key, 70)
        results = pipe.execute()

        count = results[1]
        return count >= rpm


# ---------------------------------------------------------------------------
# Auto-select backend
# ---------------------------------------------------------------------------

def _create_limiter():
    if REDIS_URL:
        try:
            rl = RedisRateLimiter(REDIS_URL)
            rl._redis.ping()
            logger.info("Rate limiter using Redis at %s", REDIS_URL)
            return rl
        except Exception as e:
            logger.warning("Redis unavailable (%s), falling back to memory rate limiter", e)
    return MemoryRateLimiter()


_limiter = _create_limiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)

    def _get_key(self, request: Request) -> str:
        return request.client.host if request.client else "__unknown__"

    async def dispatch(self, request: Request, call_next):
        if RATE_LIMIT_RPM <= 0:
            return await call_next(request)

        if request.url.path == "/health":
            return await call_next(request)

        key = self._get_key(request)
        if _limiter.is_rate_limited(key, RATE_LIMIT_RPM):
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
