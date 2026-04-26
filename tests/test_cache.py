import time
from unittest.mock import patch

from proxy.cache import MemoryCache, make_key, is_cacheable


class TestMemoryCache:
    def test_cache_hit(self):
        c = MemoryCache(max_entries=10, ttl_s=60)
        key = make_key("gpt-4", [{"role": "user", "content": "hi"}])
        c.put(key, {"choices": []}, 200, {"content-type": "application/json"})
        result = c.get(key)
        assert result is not None
        assert result["body"] == {"choices": []}

    def test_cache_miss(self):
        c = MemoryCache(max_entries=10, ttl_s=60)
        assert c.get("nonexistent") is None

    def test_different_messages_different_keys(self):
        key1 = make_key("gpt-4", [{"role": "user", "content": "hi"}])
        key2 = make_key("gpt-4", [{"role": "user", "content": "bye"}])
        assert key1 != key2

    def test_ttl_expiration(self):
        c = MemoryCache(max_entries=10, ttl_s=1)
        key = make_key("gpt-4", [{"role": "user", "content": "hi"}])
        c.put(key, {"choices": []}, 200, {})
        assert c.get(key) is not None
        time.sleep(1.1)
        assert c.get(key) is None

    def test_max_entries_eviction(self):
        c = MemoryCache(max_entries=2, ttl_s=60)
        c.put("k1", {"a": 1}, 200, {})
        c.put("k2", {"a": 2}, 200, {})
        c.put("k3", {"a": 3}, 200, {})
        assert c.get("k1") is None  # evicted
        assert c.get("k2") is not None
        assert c.get("k3") is not None

    def test_is_cacheable_stream_false(self):
        assert not is_cacheable({"model": "gpt-4", "stream": True, "messages": [{"role": "user", "content": "hi"}]})

    def test_is_cacheable_temperature_gt_zero(self):
        assert not is_cacheable({"model": "gpt-4", "temperature": 0.7, "messages": [{"role": "user", "content": "hi"}]})

    def test_is_cacheable_valid(self):
        assert is_cacheable({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})

    def test_is_cacheable_temperature_zero(self):
        assert is_cacheable({"model": "gpt-4", "temperature": 0, "messages": [{"role": "user", "content": "hi"}]})

    def test_stats(self):
        c = MemoryCache(max_entries=10, ttl_s=60)
        c.put("k1", {}, 200, {})
        c.get("k1")  # hit
        c.get("k2")  # miss
        stats = c.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1
        assert stats["hit_rate"] == 0.5

    def test_non_200_not_cached(self):
        c = MemoryCache(max_entries=10, ttl_s=60)
        c.put("k1", {"error": "bad"}, 400, {})
        assert c.get("k1") is None
