import os
import sys
import logging

logger = logging.getLogger("llmproxy.security")

AGNOSTIC_SECURITY_PATH = os.getenv(
    "AGNOSTIC_SECURITY_PATH", "/Users/parthamehta/AgnosticSecurity"
)

_pii_detector = None
_injection_detector = None
_available = False


def _load():
    global _pii_detector, _injection_detector, _available
    if _available:
        return True

    if not os.path.isdir(AGNOSTIC_SECURITY_PATH):
        logger.warning("AgnosticSecurity not found at %s — security hooks disabled", AGNOSTIC_SECURITY_PATH)
        return False

    if AGNOSTIC_SECURITY_PATH not in sys.path:
        sys.path.insert(0, AGNOSTIC_SECURITY_PATH)

    try:
        from security import pii_detector, injection_detector
        _pii_detector = pii_detector
        _injection_detector = injection_detector
        _available = True
        logger.info("Security hooks loaded from %s", AGNOSTIC_SECURITY_PATH)
        return True
    except ImportError as e:
        logger.warning("Failed to import AgnosticSecurity modules: %s — security hooks disabled", e)
        return False


def scan_inbound(messages: list[dict]) -> dict:
    """Scan inbound messages for PII and injection. Returns a report dict."""
    if not _load():
        return {"available": False}

    report = {"available": True, "pii": [], "injection": False, "injection_patterns": []}

    for msg in messages:
        content = msg.get("content", "")
        if not content:
            continue

        pii_result = _pii_detector.scan_and_redact(content)
        if pii_result.has_pii:
            report["pii"].extend(pii_result.found)

        injection_result = _injection_detector.scan(content)
        if injection_result.detected:
            report["injection"] = True
            report["injection_patterns"].extend(injection_result.patterns_matched)

    return report


def redact_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of messages with PII redacted."""
    if not _load():
        return messages

    redacted = []
    for msg in messages:
        content = msg.get("content", "")
        if content:
            result = _pii_detector.scan_and_redact(content)
            redacted.append({**msg, "content": result.redacted if hasattr(result, "redacted") else content})
        else:
            redacted.append(msg)
    return redacted


def redact_text(text: str) -> str:
    """Redact PII from a text string."""
    if not _load():
        return text
    result = _pii_detector.scan_and_redact(text)
    return result.redacted if hasattr(result, "redacted") else text


def scan_outbound(content: str) -> dict:
    """Scan outbound response content for PII leakage. Returns a report dict."""
    if not _load():
        return {"available": False}

    report = {"available": True, "pii": []}

    pii_result = _pii_detector.scan_and_redact(content)
    if pii_result.has_pii:
        report["pii"] = pii_result.found

    return report
