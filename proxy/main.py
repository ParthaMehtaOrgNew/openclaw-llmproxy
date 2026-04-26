import json
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from proxy.alerts import alert_pii_detected, alert_injection_detected
from proxy.auth import AuthMiddleware
from proxy import cache
from proxy.config import OLLAMA_BASE_URL, SECURITY_PII_MODE, SECURITY_INJECTION_MODE
from proxy.dashboard import router as dashboard_router
from proxy.fallback import get_fallback_chain, is_fallback_eligible
from proxy.keymanager import inject_backend_key
from proxy.loadbalancer import record_latency
from proxy.logger import log_request
from proxy.ratelimit import RateLimitMiddleware
from proxy.retry import request_with_retry, get_backend_timeout
from proxy.router import resolve as resolve_backend
from proxy.security import scan_inbound, scan_outbound, redact_messages, redact_text
from proxy.sizelimit import SizeLimitMiddleware
from proxy import spend
from proxy.translators import translate_request, translate_response, translate_stream_chunk, needs_translation

app = FastAPI(title="OpenClaw LLM Proxy")

# Middleware order: SizeLimit (outermost) → Auth → RateLimit (innermost)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SizeLimitMiddleware)

# Dashboard routes (must be before catch-all)
app.include_router(dashboard_router)

# Web dashboard routes
from proxy.web_dashboard import router as web_dashboard_router
app.include_router(web_dashboard_router)

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
            "url": backend.get("urls", [backend.get("url", "")])[0] if "urls" in backend else backend.get("url", ""),
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

    # Budget check
    if spend.check_budget(backend_name):
        return JSONResponse(status_code=402, content={"error": f"Monthly budget exceeded for backend '{backend_name}'"})

    inbound_scan = None
    if request_body and isinstance(request_body, dict) and "messages" in request_body:
        inbound_scan = scan_inbound(request_body["messages"])

    # Security enforcement
    if inbound_scan and inbound_scan.get("available"):
        # Injection enforcement
        if inbound_scan.get("injection") and SECURITY_INJECTION_MODE == "block":
            await alert_injection_detected(backend_name, model, inbound_scan.get("injection_patterns", []))
            log_request(method=request.method, path=f"/{path}", status_code=403,
                        latency_ms=0, request_body=request_body, inbound_scan=inbound_scan,
                        backend=backend_name)
            return JSONResponse(status_code=403, content={
                "error": "Request blocked: prompt injection detected",
                "patterns": inbound_scan.get("injection_patterns", []),
            })

        # PII enforcement
        if inbound_scan.get("pii"):
            if SECURITY_PII_MODE == "block":
                await alert_pii_detected(backend_name, model, inbound_scan["pii"], "inbound")
                log_request(method=request.method, path=f"/{path}", status_code=403,
                            latency_ms=0, request_body=request_body, inbound_scan=inbound_scan,
                            backend=backend_name)
                return JSONResponse(status_code=403, content={
                    "error": "Request blocked: PII detected in prompt",
                    "pii_types": inbound_scan["pii"],
                })
            elif SECURITY_PII_MODE == "redact":
                request_body["messages"] = redact_messages(request_body["messages"])
                body = json.dumps(request_body).encode()
                await alert_pii_detected(backend_name, model, inbound_scan["pii"], "inbound")
            else:
                # log mode — fire alert but don't block
                if inbound_scan["pii"]:
                    await alert_pii_detected(backend_name, model, inbound_scan["pii"], "inbound")

        if inbound_scan.get("injection") and SECURITY_INJECTION_MODE == "log":
            await alert_injection_detected(backend_name, model, inbound_scan.get("injection_patterns", []))

    # Inject backend-specific API key
    headers = inject_backend_key(headers, backend_name)

    is_streaming = (
        request_body
        and isinstance(request_body, dict)
        and request_body.get("stream") is True
    )

    # Build fallback chain
    fallback_chain = get_fallback_chain(backend_name)
    backends_to_try = [{"url": backend_url, "name": backend_name}] + fallback_chain

    if is_streaming:
        return await _handle_streaming_with_fallback(
            request, path, headers, body,
            request_body, backends_to_try, inbound_scan,
        )

    return await _handle_buffered_with_fallback(
        request, path, headers, body,
        request_body, backends_to_try, inbound_scan,
    )


async def _handle_buffered_with_fallback(request, path, headers, body,
                                          request_body, backends_to_try, inbound_scan):
    # Check cache first
    cache_hit = False
    cache_key = None
    if cache.is_cacheable(request_body):
        cache_key = cache.make_key(request_body["model"], request_body["messages"])
        cached = cache.get(cache_key)
        if cached:
            cache_hit = True
            log_request(
                method=request.method, path=f"/{path}",
                status_code=cached["status_code"], latency_ms=0,
                request_body=request_body, response_body=cached["body"],
                inbound_scan=inbound_scan, outbound_scan=None,
                backend="cache", cache_hit=True,
            )
            return JSONResponse(
                content=cached["body"],
                status_code=cached["status_code"],
                headers=cached["headers"],
            )

    last_error = None
    for i, backend in enumerate(backends_to_try):
        bname = backend["name"]
        timeout_s = get_backend_timeout(bname)

        # Translate request for this backend
        t_body, t_headers, t_path = translate_request(
            request_body, dict(headers), path, bname,
        )
        send_body = json.dumps(t_body).encode() if t_body and needs_translation(bname) else body
        url = f"{backend['url']}/{t_path}"

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient() as client:
                response = await request_with_retry(
                    client, request.method, url, t_headers, send_body,
                    request.query_params, timeout_s,
                )
        except httpx.TimeoutException:
            if is_fallback_eligible(exc=httpx.TimeoutException()) and i < len(backends_to_try) - 1:
                continue
            return JSONResponse(status_code=504, content={"error": f"Backend '{bname}' timed out"})
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if is_fallback_eligible(exc=exc) and i < len(backends_to_try) - 1:
                continue
            return JSONResponse(status_code=502, content={"error": f"Cannot connect to backend '{bname}': {exc}"})

        latency_ms = (time.perf_counter() - start) * 1000
        record_latency(url, latency_ms)

        # Check if we should fallback on 5xx
        if is_fallback_eligible(status_code=response.status_code) and i < len(backends_to_try) - 1:
            continue

        # Success or final backend — process response
        is_json = response.headers.get("content-type", "").startswith("application/json")
        response_body = None
        if is_json:
            try:
                response_body = response.json()
            except (json.JSONDecodeError, ValueError):
                response_body = None

        # Translate response back to OpenAI format
        if response_body and needs_translation(bname):
            response_body = translate_response(response_body, bname)

        outbound_scan = None
        if response_body and isinstance(response_body, dict):
            choices = response_body.get("choices", [])
            for choice in choices:
                content = choice.get("message", {}).get("content", "")
                if content:
                    outbound_scan = scan_outbound(content)
                    # Outbound PII enforcement
                    if outbound_scan.get("pii") and SECURITY_PII_MODE == "redact":
                        choice["message"]["content"] = redact_text(content)
                    break

        # Calculate spend
        model = request_body.get("model") if request_body and isinstance(request_body, dict) else None
        prompt_tokens = None
        completion_tokens = None
        if response_body and isinstance(response_body, dict):
            usage = response_body.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
        cost_usd = spend.calculate_and_record(bname, model, prompt_tokens, completion_tokens)

        resp_headers = {k: v for k, v in response.headers.items() if k.lower() not in FILTERED_HEADERS}

        # Cache the response
        if cache_key and response.status_code == 200 and response_body is not None:
            cache.put(cache_key, response_body, response.status_code, resp_headers)

        log_request(
            method=request.method, path=f"/{path}",
            status_code=response.status_code, latency_ms=latency_ms,
            request_body=request_body, response_body=response_body,
            inbound_scan=inbound_scan, outbound_scan=outbound_scan,
            backend=bname, cache_hit=False, cost_usd=cost_usd,
        )

        return JSONResponse(
            content=response_body if response_body is not None else response.text,
            status_code=response.status_code,
            headers=resp_headers,
        )


async def _handle_streaming_with_fallback(request, path, headers, body,
                                           request_body, backends_to_try, inbound_scan):
    for i, backend in enumerate(backends_to_try):
        bname = backend["name"]
        timeout_s = get_backend_timeout(bname)

        # Translate request for this backend
        t_body, t_headers, t_path = translate_request(
            request_body, dict(headers), path, bname, is_streaming=True,
        )
        send_body = json.dumps(t_body).encode() if t_body and needs_translation(bname) else body
        url = f"{backend['url']}/{t_path}"

        start = time.perf_counter()

        client = httpx.AsyncClient(timeout=timeout_s)
        try:
            req = client.build_request(
                method=request.method, url=url,
                headers=t_headers, content=send_body, params=request.query_params,
            )
            response = await client.send(req, stream=True)
        except httpx.TimeoutException:
            await client.aclose()
            if is_fallback_eligible(exc=httpx.TimeoutException()) and i < len(backends_to_try) - 1:
                continue
            return JSONResponse(status_code=504, content={"error": f"Backend '{bname}' timed out"})
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            await client.aclose()
            if is_fallback_eligible(exc=exc) and i < len(backends_to_try) - 1:
                continue
            return JSONResponse(status_code=502, content={"error": f"Cannot connect to backend '{bname}': {exc}"})

        # Connected successfully — stream the response
        _translate = needs_translation(bname)
        _bname = bname

        async def event_generator():
            try:
                async for chunk in response.aiter_bytes():
                    if _translate:
                        translated = translate_stream_chunk(chunk, _bname)
                        if translated:
                            yield translated
                    else:
                        yield chunk
            finally:
                await response.aclose()
                await client.aclose()
                latency_ms = (time.perf_counter() - start) * 1000
                record_latency(url, latency_ms)
                log_request(
                    method=request.method, path=f"/{path}",
                    status_code=response.status_code, latency_ms=latency_ms,
                    request_body=request_body, response_body=None,
                    inbound_scan=inbound_scan, outbound_scan=None,
                    backend=bname,
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
