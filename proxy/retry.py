import asyncio

import httpx

from proxy.router import _load_backends

DEFAULT_TIMEOUT_S = 30
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 503}
BACKOFF_BASE = 1.0


def get_backend_timeout(backend_name: str) -> float:
    backends = _load_backends()
    for _prefix, backend in backends.get("routes", {}).items():
        if backend.get("name") == backend_name:
            return backend.get("timeout_s", DEFAULT_TIMEOUT_S)
    default = backends.get("default", {})
    if default.get("name") == backend_name:
        return default.get("timeout_s", DEFAULT_TIMEOUT_S)
    return DEFAULT_TIMEOUT_S


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict,
    content: bytes,
    params,
    timeout_s: float,
) -> httpx.Response:
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=content,
                params=params,
                timeout=timeout_s,
            )
            if response.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                wait = BACKOFF_BASE * (2 ** attempt)
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
                await asyncio.sleep(wait)
                continue
            return response
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            raise
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            raise
    raise last_exc or httpx.TimeoutException("Request timed out after retries")
