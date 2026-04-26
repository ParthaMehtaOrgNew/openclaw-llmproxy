import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from proxy.logger import LOG_DIR
from proxy.router import _load_backends

SPEND_FILE = os.path.join(LOG_DIR, "spend.jsonl")


class SpendTracker:
    def __init__(self):
        self._by_backend: dict[str, float] = defaultdict(float)
        self._by_model: dict[str, float] = defaultdict(float)
        self._daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._monthly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._load_from_disk()

    def _load_from_disk(self):
        """Reload cumulative spend from spend.jsonl on startup."""
        if not os.path.exists(SPEND_FILE):
            return
        try:
            with open(SPEND_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    backend = entry.get("backend", "unknown")
                    model = entry.get("model")
                    cost = entry.get("cost_usd", 0)
                    ts = entry.get("timestamp", "")
                    self._by_backend[backend] += cost
                    if model:
                        self._by_model[model] += cost
                    if ts:
                        day = ts[:10]
                        month = ts[:7]
                        self._daily[day][backend] += cost
                        self._monthly[month][backend] += cost
        except OSError:
            pass

    def get_pricing(self, backend_name: str) -> dict | None:
        backends = _load_backends()
        for _prefix, route in backends.get("routes", {}).items():
            if route.get("name") == backend_name:
                return route.get("pricing")
        default = backends.get("default", {})
        if default.get("name") == backend_name:
            return default.get("pricing")
        return None

    def calculate_cost(self, backend_name: str, prompt_tokens: int | None,
                       completion_tokens: int | None) -> float | None:
        pricing = self.get_pricing(backend_name)
        if not pricing:
            return None
        pt = (prompt_tokens or 0) / 1000.0 * pricing.get("prompt", 0)
        ct = (completion_tokens or 0) / 1000.0 * pricing.get("completion", 0)
        return round(pt + ct, 6)

    def record(self, backend_name: str, model: str | None, cost_usd: float):
        self._by_backend[backend_name] += cost_usd
        if model:
            self._by_model[model] += cost_usd
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        self._daily[today][backend_name] += cost_usd
        self._monthly[month][backend_name] += cost_usd

        # Persist
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "backend": backend_name,
                "model": model,
                "cost_usd": cost_usd,
            }
            with open(SPEND_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def check_budget(self, backend_name: str) -> bool:
        backends = _load_backends()
        for _prefix, route in backends.get("routes", {}).items():
            if route.get("name") == backend_name:
                budget = route.get("monthly_budget_usd")
                if budget is not None:
                    month = datetime.now(timezone.utc).strftime("%Y-%m")
                    spent = self._monthly.get(month, {}).get(backend_name, 0)
                    return spent >= budget
                return False
        return False

    def get_summary(self) -> dict:
        total = sum(self._by_backend.values())
        return {
            "total_usd": round(total, 6),
            "by_backend": {k: round(v, 6) for k, v in self._by_backend.items()},
            "by_model": {k: round(v, 6) for k, v in self._by_model.items()},
            "daily": {
                day: {k: round(v, 6) for k, v in backends.items()}
                for day, backends in sorted(self._daily.items())
            },
        }


_tracker = SpendTracker()


def calculate_and_record(backend_name: str, model: str | None,
                         prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    cost = _tracker.calculate_cost(backend_name, prompt_tokens, completion_tokens)
    if cost is not None and cost > 0:
        _tracker.record(backend_name, model, cost)
    return cost


def check_budget(backend_name: str) -> bool:
    return _tracker.check_budget(backend_name)


def get_spend_summary() -> dict:
    return _tracker.get_summary()
