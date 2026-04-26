import json
import os
import time
from datetime import datetime, timezone

from proxy.config import LOG_REDACT_BODIES

LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "requests.jsonl")


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def log_request(
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    request_body: dict | None = None,
    response_body: dict | None = None,
    inbound_scan: dict | None = None,
    outbound_scan: dict | None = None,
    backend: str | None = None,
    cache_hit: bool | None = None,
    cost_usd: float | None = None,
):
    _ensure_log_dir()

    model = None
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None

    if request_body and isinstance(request_body, dict):
        model = request_body.get("model")

    if response_body and isinstance(response_body, dict):
        usage = response_body.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "path": path,
        "backend": backend,
        "model": model,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "inbound_scan": inbound_scan,
        "outbound_scan": outbound_scan,
        "cache_hit": cache_hit,
        "cost_usd": cost_usd,
    }

    # Include request/response bodies unless redaction is enabled
    if not LOG_REDACT_BODIES:
        if request_body and isinstance(request_body, dict):
            entry["request_messages"] = request_body.get("messages")
        if response_body and isinstance(response_body, dict):
            choices = response_body.get("choices", [])
            if choices:
                entry["response_content"] = choices[0].get("message", {}).get("content")

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
