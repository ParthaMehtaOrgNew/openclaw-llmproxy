import hashlib
import json
import logging
import time
from collections import OrderedDict

from proxy.config import CACHE_TTL_S, CACHE_MAX_ENTRIES, REDIS_URL, REDIS_CLUSTER

logger = logging.getLogger("llmproxy.cache")


# ---------------------------------------------------------------------------
# In-memory LRU cache (default)
# ---------------------------------------------------------------------------

class MemoryCache:
    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, ttl_s: int = CACHE_TTL_S):
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> dict | None:
        if key not in self._cache:
            self._misses += 1
            return None
        ts, entry = self._cache[key]
        if time.time() - ts > self._ttl_s:
            del self._cache[key]
            self._misses += 1
            return None
        self._cache.move_to_end(key)
        self._hits += 1
        return entry

    def put(self, key: str, response_body: dict, status_code: int, headers: dict):
        if status_code != 200:
            return
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (time.time(), {
            "body": response_body,
            "status_code": status_code,
            "headers": headers,
        })
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
            "backend": "memory",
        }


# ---------------------------------------------------------------------------
# Redis cache (production)
# ---------------------------------------------------------------------------

class RedisCache:
    def __init__(self, redis_url: str, ttl_s: int = CACHE_TTL_S, cluster: bool = False):
        import redis as redis_lib
        if cluster:
            from redis.cluster import RedisCluster
            self._redis = RedisCluster.from_url(redis_url, decode_responses=True)
        else:
            self._redis = redis_lib.from_url(redis_url, decode_responses=True)
        self._ttl_s = ttl_s
        self._prefix = "llmproxy:cache:"
        self._hits_key = "llmproxy:cache_hits"
        self._misses_key = "llmproxy:cache_misses"

    def get(self, key: str) -> dict | None:
        raw = self._redis.get(self._prefix + key)
        if raw is None:
            self._redis.incr(self._misses_key)
            return None
        self._redis.incr(self._hits_key)
        return json.loads(raw)

    def put(self, key: str, response_body: dict, status_code: int, headers: dict):
        if status_code != 200:
            return
        entry = {"body": response_body, "status_code": status_code, "headers": headers}
        self._redis.setex(self._prefix + key, self._ttl_s, json.dumps(entry))

    def stats(self) -> dict:
        hits = int(self._redis.get(self._hits_key) or 0)
        misses = int(self._redis.get(self._misses_key) or 0)
        total = hits + misses
        size = len(self._redis.keys(self._prefix + "*"))
        return {
            "size": size,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 4) if total > 0 else 0.0,
            "backend": "redis",
        }


# ---------------------------------------------------------------------------
# Module-level interface (auto-selects backend)
# ---------------------------------------------------------------------------

def _create_cache():
    if REDIS_URL:
        try:
            c = RedisCache(REDIS_URL, cluster=REDIS_CLUSTER)
            c._redis.ping()
            mode = "Redis Cluster" if REDIS_CLUSTER else "Redis"
            logger.info("Cache using %s at %s", mode, REDIS_URL)
            return c
        except Exception as e:
            logger.warning("Redis unavailable (%s), falling back to memory cache", e)
    return MemoryCache()


_cache = _create_cache()


def get(key: str) -> dict | None:
    return _cache.get(key)


def put(key: str, response_body: dict, status_code: int, headers: dict):
    _cache.put(key, response_body, status_code, headers)


def make_key(model: str, messages: list) -> str:
    raw = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def is_cacheable(request_body: dict | None) -> bool:
    if not request_body or not isinstance(request_body, dict):
        return False
    if request_body.get("stream") is True:
        return False
    if (request_body.get("temperature") or 0) > 0:
        return False
    if "messages" not in request_body or "model" not in request_body:
        return False
    return True


def cache_stats() -> dict:
    return _cache.stats()
