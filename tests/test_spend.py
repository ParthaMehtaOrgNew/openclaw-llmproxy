import json
import os
import tempfile
from unittest.mock import patch

from proxy.spend import SpendTracker
from fastapi.testclient import TestClient
from proxy.main import app


class TestSpendTracker:
    def _write_backends(self, tmp, data):
        path = os.path.join(tmp, "backends.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_cost_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_backends(tmp, {
                "default": {"url": "http://localhost:11434", "name": "ollama"},
                "routes": {
                    "gpt-": {
                        "url": "https://api.openai.com",
                        "name": "openai",
                        "pricing": {"prompt": 0.03, "completion": 0.06},
                    }
                },
            })
            with patch("proxy.router.BACKENDS_FILE", path):
                tracker = SpendTracker()
                cost = tracker.calculate_cost("openai", 1000, 500)
                # (1000/1000 * 0.03) + (500/1000 * 0.06) = 0.03 + 0.03 = 0.06
                assert cost == 0.06

    def test_no_pricing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_backends(tmp, {
                "default": {"url": "http://localhost:11434", "name": "ollama"},
                "routes": {},
            })
            with patch("proxy.router.BACKENDS_FILE", path):
                tracker = SpendTracker()
                assert tracker.calculate_cost("ollama", 100, 50) is None

    def test_spend_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("proxy.spend.SPEND_FILE", os.path.join(tmp, "spend.jsonl")):
                tracker = SpendTracker()
                tracker.record("openai", "gpt-4", 0.05)
                tracker.record("openai", "gpt-4", 0.03)
                tracker.record("anthropic", "claude-3", 0.02)
                summary = tracker.get_summary()
                assert summary["total_usd"] == 0.1
                assert summary["by_backend"]["openai"] == 0.08
                assert summary["by_backend"]["anthropic"] == 0.02
                assert summary["by_model"]["gpt-4"] == 0.08

    def test_spend_endpoint(self):
        client = TestClient(app)
        resp = client.get("/spend")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_usd" in data
        assert "by_backend" in data

    def test_budget_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_backends(tmp, {
                "default": {"url": "http://localhost:11434", "name": "ollama"},
                "routes": {
                    "gpt-": {
                        "url": "https://api.openai.com",
                        "name": "openai",
                        "monthly_budget_usd": 0.001,
                    }
                },
            })
            with patch("proxy.router.BACKENDS_FILE", path), \
                 patch("proxy.spend.SPEND_FILE", os.path.join(tmp, "spend.jsonl")):
                tracker = SpendTracker()
                assert not tracker.check_budget("openai")
                tracker.record("openai", "gpt-4", 0.01)
                assert tracker.check_budget("openai")
