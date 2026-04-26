"""Per-backend API key management.

Clients send the proxy's API key. The proxy injects the correct
backend-specific API key from backends.json before forwarding.
"""

from proxy.router import _load_backends


def inject_backend_key(headers: dict, backend_name: str) -> dict:
    """Replace or inject the backend-specific API key into request headers.

    Reads 'api_key' from the backend's config in backends.json.
    If no api_key is configured, headers pass through unchanged.
    """
    backends = _load_backends()

    api_key = _find_api_key(backends, backend_name)
    if not api_key:
        return headers

    new_headers = dict(headers)
    new_headers["authorization"] = f"Bearer {api_key}"
    return new_headers


def _find_api_key(backends: dict, backend_name: str) -> str | None:
    """Look up api_key for a backend by name."""
    for _prefix, route in backends.get("routes", {}).items():
        if route.get("name") == backend_name:
            return route.get("api_key")
    default = backends.get("default", {})
    if default.get("name") == backend_name:
        return default.get("api_key")
    return None
