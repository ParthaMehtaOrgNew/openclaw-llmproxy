"""Webhook alerts for security events (PII detection, injection attacks)."""

import json
import logging
from datetime import datetime, timezone

import httpx

from proxy.config import SECURITY_WEBHOOK_URL

logger = logging.getLogger("llmproxy.alerts")


async def send_alert(event_type: str, details: dict):
    """Fire a webhook alert if SECURITY_WEBHOOK_URL is configured."""
    if not SECURITY_WEBHOOK_URL:
        return

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "proxy": "openclaw-llmproxy",
        **details,
    }

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                SECURITY_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        logger.warning("Failed to send security alert: %s", exc)


async def alert_pii_detected(backend: str, model: str | None, pii_types: list[str], direction: str):
    """Alert when PII is detected in a request or response."""
    await send_alert("pii_detected", {
        "backend": backend,
        "model": model,
        "pii_types": pii_types,
        "direction": direction,
    })


async def alert_injection_detected(backend: str, model: str | None, patterns: list[str]):
    """Alert when a prompt injection attack is detected."""
    await send_alert("injection_detected", {
        "backend": backend,
        "model": model,
        "patterns": patterns,
    })


async def alert_budget_exceeded(backend: str):
    """Alert when a backend's monthly budget is exceeded."""
    await send_alert("budget_exceeded", {
        "backend": backend,
    })
