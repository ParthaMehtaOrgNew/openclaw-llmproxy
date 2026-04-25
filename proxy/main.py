import json
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from proxy.auth import AuthMiddleware
from proxy.config import OLLAMA_BASE_URL
from proxy.dashboard import router as dashboard_router
from proxy.logger import log_request
from proxy.ratelimit import RateLimitMiddleware
from proxy.retry import request_with_retry, get_backend_timeout
from proxy.router import resolve as resolve_backend
from proxy.security import scan_inbound, scan_outbound
from proxy.sizelimit import SizeLimitMiddleware

app = FastAPI(title="OpenClaw LLM Proxy")

# Middleware order: SizeLimit (outermost) → Auth → RateLimit (innermost)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SizeLimitMiddleware)

# Dashboard routes (must be before catch-all)
app.include_router(dashboard_router)

FILTERED_HEADERS = {"content-encoding", "transfer-encoding", "content-length"}


@app.get("/health")
async def health():
    result = {"status": "healthy", "backends": {}}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            result["backends"]["ollama"] = {"status": "reachable", "models": models}
        else:
            result["backends"]["ollama"] = {"status": "error", "detail": f"returned {resp.status_code}"}
            result["status"] = "degraded"
    except httpx.ConnectError:
        result["backends"]["ollama"] = {"status": "unreachable"}
        result["status"] = "degraded"

    from proxy.router import _load_backends
    backends = _load_backends()
    for prefix, backend in backends.get("routes", {}).items():
        result["backends"][backend.get("name", prefix)] = {
            "status": "configured",
            "prefix": prefix,
            "url": backend["url"],
        }

    return result


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    headers = dict(request.headers)
    headers.pop("host", None)
    body = await request.body()

    request_body = None
    if body:
        try:
            request_body = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    model = request_body.get("model") if request_body and isinstance(request_body, dict) else None
    backend_url, backend_name = resolve_backend(model, path)
    url = f"{backend_url}/{path}"

    inbound_scan = None
    if request_body and isinstance(request_body, dict) and "messages" in request_body:
        inbound_scan = scan_inbound(request_body["messages"])

    is_streaming = (
        request_body
        and isinstance(request_body, dict)
        and request_body.get("stream") is True
    )

    if is_streaming:
        return await _handle_streaming(
            request, path, url, headers, body,
            request_body, backend_name, inbound_scan,
        )

    return await _handle_buffered(
        request, path, url, headers, body,
        request_body, backend_name, inbound_scan,
    )


async def _handle_buffered(request, path, url, headers, body,
                           request_body, backend_name, inbound_scan):
    start = time.perf_counter()
    timeout_s = get_backend_timeout(backend_name)

    try:
        async with httpx.AsyncClient() as client:
            response = await request_with_retry(
                client, request.method, url, headers, body,
                request.query_params, timeout_s,
            )
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"error": "Backend timed out"})
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        return JSONResponse(status_code=502, content={"error": f"Cannot connect to backend '{backend_name}': {exc}"})

    latency_ms = (time.perf_counter() - start) * 1000

    is_json = response.headers.get("content-type", "").startswith("application/json")
    response_body = None
    if is_json:
        try:
            response_body = response.json()
        except (json.JSONDecodeError, ValueError):
            response_body = None

    outbound_scan = None
    if response_body and isinstance(response_body, dict):
        choices = response_body.get("choices", [])
        for choice in choices:
            content = choice.get("message", {}).get("content", "")
            if content:
                outbound_scan = scan_outbound(content)
                break

    log_request(
        method=request.method,
        path=f"/{path}",
        status_code=response.status_code,
        latency_ms=latency_ms,
        request_body=request_body,
        response_body=response_body,
        inbound_scan=inbound_scan,
        outbound_scan=outbound_scan,
        backend=backend_name,
    )

    return JSONResponse(
        content=response_body if response_body is not None else response.text,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in FILTERED_HEADERS},
    )


async def _handle_streaming(request, path, url, headers, body,
                            request_body, backend_name, inbound_scan):
    start = time.perf_counter()
    timeout_s = get_backend_timeout(backend_name)

    client = httpx.AsyncClient(timeout=timeout_s)
    try:
        req = client.build_request(
            method=request.method, url=url,
            headers=headers, content=body, params=request.query_params,
        )
        response = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return JSONResponse(status_code=504, content={"error": "Backend timed out"})
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        await client.aclose()
        return JSONResponse(status_code=502, content={"error": f"Cannot connect to backend '{backend_name}': {exc}"})

    async def event_generator():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            latency_ms = (time.perf_counter() - start) * 1000
            log_request(
                method=request.method,
                path=f"/{path}",
                status_code=response.status_code,
                latency_ms=latency_ms,
                request_body=request_body,
                response_body=None,
                inbound_scan=inbound_scan,
                outbound_scan=None,
                backend=backend_name,
            )

    resp_headers = {
        k: v for k, v in response.headers.items()
        if k.lower() not in FILTERED_HEADERS
    }

    return StreamingResponse(
        event_generator(),
        status_code=response.status_code,
        headers=resp_headers,
        media_type=response.headers.get("content-type", "text/event-stream"),
    )
