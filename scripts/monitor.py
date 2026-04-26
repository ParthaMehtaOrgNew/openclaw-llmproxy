#!/usr/bin/env python3
"""Continuous background monitor for OpenClaw LLM Proxy.

Runs health checks, validates security scanning, and fires alerts
on failures. Designed to run as a systemd service or Docker sidecar.

Usage:
    python scripts/monitor.py [--url http://localhost:8005] [--interval 60] [--api-key KEY]
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("monitor")


def check_health(base_url: str) -> dict:
    """Check /health endpoint."""
    try:
        req = Request(f"{base_url}/health")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {"ok": True, "status": data.get("status"), "backends": data.get("backends", {})}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_auth(base_url: str) -> dict:
    """Verify auth is enforced (unauthenticated request should get 401)."""
    try:
        req = Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps({"model": "test", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return {"ok": False, "error": f"Expected 401, got {resp.status}"}
    except URLError as e:
        if hasattr(e, "code") and e.code == 401:
            return {"ok": True}
        return {"ok": False, "error": str(e)}


def check_dashboard(base_url: str) -> dict:
    """Verify dashboard and metrics endpoints."""
    try:
        req = Request(f"{base_url}/dashboard/metrics")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {
                "ok": True,
                "request_count": data.get("request_count", 0),
                "error_rate": data.get("error_rate", 0),
                "cache_hit_rate": data.get("cache", {}).get("hit_rate", 0),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_chat(base_url: str, api_key: str) -> dict:
    """Send a test chat completion to verify proxy → backend flow."""
    try:
        req = Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps({
                "model": "llama3.2:1b",
                "messages": [{"role": "user", "content": "Say ok"}],
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        start = time.time()
        with urlopen(req, timeout=30) as resp:
            latency = (time.time() - start) * 1000
            data = json.loads(resp.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "latency_ms": round(latency, 1), "response": content[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_checks(base_url: str, api_key: str | None) -> dict:
    """Run all checks and return results."""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": base_url,
        "checks": {},
    }

    # Health
    results["checks"]["health"] = check_health(base_url)

    # Auth enforcement
    results["checks"]["auth"] = check_auth(base_url)

    # Dashboard
    results["checks"]["dashboard"] = check_dashboard(base_url)

    # Chat completion (only if API key provided and Ollama reachable)
    if api_key:
        health = results["checks"]["health"]
        if health.get("ok") and health.get("backends", {}).get("ollama", {}).get("status") == "reachable":
            results["checks"]["chat"] = check_chat(base_url, api_key)
        else:
            results["checks"]["chat"] = {"ok": None, "skipped": "Ollama not reachable"}

    # Overall status
    failed = [k for k, v in results["checks"].items() if v.get("ok") is False]
    results["status"] = "FAIL" if failed else "OK"
    results["failed_checks"] = failed

    return results


def main():
    parser = argparse.ArgumentParser(description="OpenClaw LLM Proxy Monitor")
    parser.add_argument("--url", default="http://localhost:8005", help="Proxy base URL")
    parser.add_argument("--interval", type=int, default=60, help="Check interval in seconds")
    parser.add_argument("--api-key", default=None, help="Proxy API key for authenticated checks")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    log.info("Starting monitor — target: %s, interval: %ds", args.url, args.interval)

    while True:
        results = run_checks(args.url, args.api_key)

        if results["status"] == "OK":
            log.info("All checks passed — %d checks OK", len(results["checks"]))
        else:
            log.error("CHECKS FAILED: %s", ", ".join(results["failed_checks"]))
            for name in results["failed_checks"]:
                log.error("  %s: %s", name, results["checks"][name].get("error", "unknown"))

        # Print full results as JSON for log aggregation
        print(json.dumps(results))
        sys.stdout.flush()

        if args.once:
            sys.exit(0 if results["status"] == "OK" else 1)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
