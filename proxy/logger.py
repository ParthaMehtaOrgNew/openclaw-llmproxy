import json
import logging
import os
import time
from datetime import datetime, timezone

from proxy.config import LOG_REDACT_BODIES, DATABASE_URL

logger = logging.getLogger("llmproxy.logger")

LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "requests.jsonl")


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# PostgreSQL logger (production)
# ---------------------------------------------------------------------------

_pg_pool = None


def _init_pg():
    global _pg_pool
    if _pg_pool is not None or not DATABASE_URL:
        return
    try:
        import psycopg2
        from psycopg2 import pool
        _pg_pool = pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        conn = _pg_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS requests (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        method VARCHAR(10),
                        path TEXT,
                        backend VARCHAR(50),
                        model VARCHAR(100),
                        status_code INT,
                        latency_ms FLOAT,
                        prompt_tokens INT,
                        completion_tokens INT,
                        total_tokens INT,
                        inbound_scan JSONB,
                        outbound_scan JSONB,
                        cache_hit BOOLEAN,
                        cost_usd FLOAT,
                        request_messages JSONB,
                        response_content TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_backend ON requests(backend)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)")
            conn.commit()
        finally:
            _pg_pool.putconn(conn)
        logger.info("Logger using PostgreSQL at %s", DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL)
    except Exception as e:
        logger.warning("PostgreSQL unavailable (%s), falling back to JSONL", e)
        _pg_pool = None


def _log_to_pg(entry: dict):
    if not _pg_pool:
        return False
    try:
        conn = _pg_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO requests (
                        timestamp, method, path, backend, model, status_code,
                        latency_ms, prompt_tokens, completion_tokens, total_tokens,
                        inbound_scan, outbound_scan, cache_hit, cost_usd,
                        request_messages, response_content
                    ) VALUES (
                        %(timestamp)s, %(method)s, %(path)s, %(backend)s, %(model)s,
                        %(status_code)s, %(latency_ms)s, %(prompt_tokens)s,
                        %(completion_tokens)s, %(total_tokens)s,
                        %(inbound_scan)s, %(outbound_scan)s, %(cache_hit)s,
                        %(cost_usd)s, %(request_messages)s, %(response_content)s
                    )
                """, {
                    **entry,
                    "inbound_scan": json.dumps(entry.get("inbound_scan")) if entry.get("inbound_scan") else None,
                    "outbound_scan": json.dumps(entry.get("outbound_scan")) if entry.get("outbound_scan") else None,
                    "request_messages": json.dumps(entry.get("request_messages")) if entry.get("request_messages") else None,
                })
            conn.commit()
        finally:
            _pg_pool.putconn(conn)
        return True
    except Exception as e:
        logger.warning("Failed to log to PostgreSQL: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

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

    # Try PostgreSQL first, fall back to JSONL
    _init_pg()
    if _pg_pool and _log_to_pg(entry):
        return

    # JSONL fallback
    _ensure_log_dir()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
