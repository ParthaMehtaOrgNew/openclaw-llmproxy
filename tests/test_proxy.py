import json
import os
import tempfile
import time
from unittest.mock import patch, AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.main import app
from proxy.router import resolve, _load_backends
from proxy.retry import get_backend_timeout


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------

class TestRouter:
    """Tests for model-prefix routing logic."""

    def _write_backends(self, tmp, data):
        path = os.path.join(tmp, "backends.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_gpt_routes_to_openai(self):
        url, name = resolve("gpt-4", "/v1/chat/completions")
        assert name == "openai"
        assert "openai" in url

    def test_claude_routes_to_anthropic(self):
        url, name = resolve("claude-3-opus", "/v1/chat/completions")
        assert name == "anthropic"
        assert "anthropic" in url

    def test_gemini_routes_to_google(self):
        url, name = resolve("gemini-pro", "/v1/chat/completions")
        assert name == "google"
        assert "googleapis" in url

    def test_vllm_routes_to_localhost(self):
        url, name = resolve("vllm/mistral", "/v1/chat/completions")
        assert name == "vllm"
        assert "8080" in url

    def test_unknown_model_routes_to_default(self):
        url, name = resolve("llama3.2:1b", "/v1/chat/completions")
        assert name == "ollama"
        assert "11434" in url

    def test_none_model_routes_to_default(self):
        url, name = resolve(None, "/v1/chat/completions")
        assert name == "ollama"

    def test_custom_backends_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_backends(tmp, {
                "default": {"url": "http://custom:9999", "name": "custom"},
                "routes": {
                    "test-": {"url": "http://test:1234", "name": "testbackend"}
                },
            })
            with patch("proxy.router.BACKENDS_FILE", path):
                url, name = resolve("test-model", "/v1/chat/completions")
                assert name == "testbackend"
                assert "1234" in url

                url, name = resolve("other-model", "/v1/chat/completions")
                assert name == "custom"

    def test_load_backends_missing_file(self):
        with patch("proxy.router.BACKENDS_FILE", "/nonexistent/backends.json"):
            result = _load_backends()
            assert result == {}


# ---------------------------------------------------------------------------
# Health endpoint tests
# ---------------------------------------------------------------------------

class TestHealth:
    """Tests for the /health endpoint."""

    client = TestClient(app)

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "backends" in data

    def test_health_lists_configured_backends(self):
        resp = self.client.get("/health")
        data = resp.json()
        backends = data["backends"]
        assert "openai" in backends
        assert "anthropic" in backends
        assert "google" in backends
        assert "vllm" in backends
        for name in ("openai", "anthropic", "google", "vllm"):
            assert backends[name]["status"] == "configured"
            assert "url" in backends[name]
            assert "prefix" in backends[name]

    def test_health_shows_ollama_status(self):
        resp = self.client.get("/health")
        data = resp.json()
        assert "ollama" in data["backends"]
        assert data["backends"]["ollama"]["status"] in ("reachable", "unreachable", "error")


# ---------------------------------------------------------------------------
# Proxy endpoint tests
# ---------------------------------------------------------------------------

class TestProxyEndpoint:
    """Tests for the catch-all proxy endpoint."""

    client = TestClient(app)

    def test_proxy_forwards_to_ollama(self):
        """Integration test: forwards to Ollama if it's running."""
        resp = self.client.post(
            "/v1/chat/completions",
            json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "Say hi"}]},
        )
        # Ollama may or may not be running; just check we get a response (not a 500 from our code)
        assert resp.status_code in (200, 404, 502, 504)


# ---------------------------------------------------------------------------
# Logger tests
# ---------------------------------------------------------------------------

class TestLogger:
    """Tests for JSONL request logging."""

    def test_log_request_writes_jsonl(self):
        from proxy.logger import log_request

        with tempfile.TemporaryDirectory() as tmp:
            log_file = os.path.join(tmp, "test.jsonl")
            with patch("proxy.logger.LOG_FILE", log_file), \
                 patch("proxy.logger.LOG_DIR", tmp):
                log_request(
                    method="POST",
                    path="/v1/chat/completions",
                    status_code=200,
                    latency_ms=123.45,
                    request_body={"model": "gpt-4"},
                    response_body={"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
                    backend="openai",
                )

                with open(log_file) as f:
                    line = f.readline()
                    entry = json.loads(line)

                assert entry["method"] == "POST"
                assert entry["path"] == "/v1/chat/completions"
                assert entry["backend"] == "openai"
                assert entry["model"] == "gpt-4"
                assert entry["status_code"] == 200
                assert entry["latency_ms"] == 123.45
                assert entry["prompt_tokens"] == 10
                assert entry["completion_tokens"] == 5
                assert entry["total_tokens"] == 15

    def test_log_request_handles_missing_fields(self):
        from proxy.logger import log_request

        with tempfile.TemporaryDirectory() as tmp:
            log_file = os.path.join(tmp, "test.jsonl")
            with patch("proxy.logger.LOG_FILE", log_file), \
                 patch("proxy.logger.LOG_DIR", tmp):
                log_request(
                    method="GET",
                    path="/health",
                    status_code=200,
                    latency_ms=5.0,
                )

                with open(log_file) as f:
                    entry = json.loads(f.readline())

                assert entry["model"] is None
                assert entry["backend"] is None
                assert entry["prompt_tokens"] is None


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    """Tests for bearer token authentication middleware."""

    def test_health_exempt_from_auth(self):
        with patch("proxy.auth.PROXY_API_KEY", "secret123"):
            client = TestClient(app)
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_missing_token_returns_401(self):
        with patch("proxy.auth.PROXY_API_KEY", "secret123"):
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status_code == 401

    def test_wrong_token_returns_401(self):
        with patch("proxy.auth.PROXY_API_KEY", "secret123"):
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert resp.status_code == 401

    def test_correct_token_passes(self):
        with patch("proxy.auth.PROXY_API_KEY", "secret123"):
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer secret123"},
            )
            # Should pass auth — may get 200 or connection error to Ollama
            assert resp.status_code != 401

    def test_no_key_configured_passes_all(self):
        with patch("proxy.auth.PROXY_API_KEY", ""):
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Rate limit tests
# ---------------------------------------------------------------------------

class TestRateLimit:
    """Tests for in-memory rate limiting middleware."""

    def test_under_limit_passes(self):
        with patch("proxy.ratelimit.RATE_LIMIT_RPM", 100):
            client = TestClient(app)
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_over_limit_returns_429(self):
        with patch("proxy.ratelimit.RATE_LIMIT_RPM", 2):
            client = TestClient(app)
            # Reset the rate limiter state
            from proxy.ratelimit import RateLimitMiddleware
            for middleware in app.user_middleware:
                if middleware.cls is RateLimitMiddleware:
                    break

            # Fire requests — health is exempt so use proxy path
            results = []
            for _ in range(4):
                resp = client.post(
                    "/v1/chat/completions",
                    json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}]},
                )
                results.append(resp.status_code)

            assert 429 in results

    def test_health_exempt_from_rate_limit(self):
        with patch("proxy.ratelimit.RATE_LIMIT_RPM", 1):
            client = TestClient(app)
            for _ in range(5):
                resp = client.get("/health")
                assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Size limit tests
# ---------------------------------------------------------------------------

class TestSizeLimit:
    """Tests for request body size limit middleware."""

    def test_small_payload_passes(self):
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_oversized_payload_returns_413(self):
        with patch("proxy.sizelimit.MAX_REQUEST_SIZE_MB", 0.0001):  # ~100 bytes
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "llama3.2:1b", "messages": [{"role": "user", "content": "x" * 200}]},
            )
            assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Retry / timeout tests
# ---------------------------------------------------------------------------

class TestRetry:
    """Tests for retry logic and backend timeout config."""

    def test_backend_timeout_from_config(self):
        timeout = get_backend_timeout("ollama")
        assert timeout == 60

    def test_backend_timeout_from_routes(self):
        timeout = get_backend_timeout("openai")
        assert timeout == 30

    def test_unknown_backend_uses_default(self):
        timeout = get_backend_timeout("nonexistent")
        assert timeout == 30  # DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Dashboard / log viewer tests
# ---------------------------------------------------------------------------

class TestDashboard:
    """Tests for the GET /logs endpoint."""

    def _write_test_logs(self, tmp):
        log_file = os.path.join(tmp, "requests.jsonl")
        entries = [
            {"timestamp": "2026-04-24T10:00:00+00:00", "method": "POST", "path": "/v1/chat/completions",
             "backend": "ollama", "model": "llama3.2:1b", "status_code": 200, "latency_ms": 100},
            {"timestamp": "2026-04-25T12:00:00+00:00", "method": "POST", "path": "/v1/chat/completions",
             "backend": "openai", "model": "gpt-4", "status_code": 200, "latency_ms": 500},
            {"timestamp": "2026-04-25T14:00:00+00:00", "method": "POST", "path": "/v1/chat/completions",
             "backend": "anthropic", "model": "claude-3-opus", "status_code": 200, "latency_ms": 300},
        ]
        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return log_file

    def test_logs_returns_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = self._write_test_logs(tmp)
            with patch("proxy.dashboard.LOG_FILE", log_file):
                client = TestClient(app)
                resp = client.get("/logs")
                assert resp.status_code == 200
                data = resp.json()
                assert data["total"] == 3

    def test_logs_filter_by_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = self._write_test_logs(tmp)
            with patch("proxy.dashboard.LOG_FILE", log_file):
                client = TestClient(app)
                resp = client.get("/logs?backend=openai")
                data = resp.json()
                assert data["total"] == 1
                assert data["entries"][0]["backend"] == "openai"

    def test_logs_filter_by_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = self._write_test_logs(tmp)
            with patch("proxy.dashboard.LOG_FILE", log_file):
                client = TestClient(app)
                resp = client.get("/logs?model=gpt-4")
                data = resp.json()
                assert data["total"] == 1
                assert data["entries"][0]["model"] == "gpt-4"

    def test_logs_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = self._write_test_logs(tmp)
            with patch("proxy.dashboard.LOG_FILE", log_file):
                client = TestClient(app)
                resp = client.get("/logs?limit=2")
                data = resp.json()
                assert data["total"] == 2

    def test_logs_since_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = self._write_test_logs(tmp)
            with patch("proxy.dashboard.LOG_FILE", log_file):
                client = TestClient(app)
                resp = client.get("/logs?since=2026-04-25")
                data = resp.json()
                assert data["total"] == 2
                for entry in data["entries"]:
                    assert entry["timestamp"] >= "2026-04-25"

    def test_logs_empty_file(self):
        with patch("proxy.dashboard.LOG_FILE", "/nonexistent/path.jsonl"):
            client = TestClient(app)
            resp = client.get("/logs")
            data = resp.json()
            assert data["entries"] == []
            assert data["total"] == 0
