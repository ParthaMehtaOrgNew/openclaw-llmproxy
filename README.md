# OpenClaw LLM Proxy

A lightweight, configurable reverse proxy for routing LLM API requests to multiple backends — OpenAI, Anthropic, Google, vLLM, Ollama, and OpenClaw — through a single endpoint.

Built for [OpenClaw](https://github.com/ParthaMehtaOrg), the open-source autonomous AI agent. Point OpenClaw at this proxy and get unified access to every LLM backend with auth, rate limiting, PII scanning, and logging — out of the box.

## Features

- **Model-prefix routing** — Requests are routed based on model name prefixes defined in `backends.json`. No code changes needed to add or remove backends.
- **Bearer token authentication** — Protect your proxy with `PROXY_API_KEY`. Disabled when unset (for local dev).
- **SSE streaming** — Full pass-through streaming support for `"stream": true` requests.
- **Rate limiting** — In-memory sliding window rate limiter, per-IP, configurable via `RATE_LIMIT_RPM`.
- **Retry with backoff** — Automatic retry on 429/503 with exponential backoff. Per-backend timeouts configurable in `backends.json`.
- **Request size limits** — Reject oversized payloads with `MAX_REQUEST_SIZE_MB`.
- **PII & injection scanning** — Inbound prompt scanning for PII and injection attacks, outbound response scanning for PII leakage (via [AgnosticSecurity](https://github.com/ParthaMehtaOrg/AgnosticSecurity)).
- **Security enforcement modes** — `SECURITY_PII_MODE=block` (403 on PII), `redact` (auto-scrub before forwarding), or `log`. Same for injection.
- **API format translation** — Clients send OpenAI format for any backend. Proxy auto-translates to Anthropic Messages API and Google Gemini API.
- **Per-backend API key management** — Add `"api_key"` in `backends.json`. Clients only need the proxy key.
- **Webhook security alerts** — `SECURITY_WEBHOOK_URL` fires JSON to Slack/PagerDuty on PII, injection, or budget events.
- **Log body redaction** — `LOG_REDACT_BODIES=true` excludes prompt/response text from logs for compliance.
- **JSONL request logging** — Every request logged with backend, model, latency, token usage, and security scan results.
- **Log viewer API** — `GET /logs` endpoint with filtering by backend, model, date, and limit.
- **Health checks** — `GET /health` shows backend reachability and configured routes.
- **Model fallback chains** — If a backend fails (5xx/timeout), automatically try the next backend in the chain. Configured per route.
- **Response caching** — In-memory LRU cache with TTL for non-streaming, deterministic requests. Saves money on repeated prompts.
- **Spend tracking & budgets** — Per-backend cost calculation, cumulative tracking, `GET /spend` endpoint, and monthly budget enforcement (returns 402 when exceeded).
- **Load balancing** — Round-robin, random, or least-latency strategies across multiple URLs per backend.
- **Live web dashboard** — `GET /dashboard` serves a real-time HTML dashboard with request count, latency, error rate, cache stats, and spend by backend.
- **Prometheus /metrics** — Prometheus-compatible metrics endpoint for Grafana/Datadog. Request counts, latency histograms, token usage, spend, cache hits, security events.
- **Multi-tenancy** — Per-team API keys with own rate limits, budgets, and backend/model restrictions via `tenants.json`.
- **Request tracing** — `X-Request-Id` correlation IDs on every request/response for distributed tracing (Jaeger, Datadog).
- **Helm chart** — One-command Kubernetes deployment: `helm install openclaw ./helm/openclaw-proxy`.
- **Python SDK** — `from sdk import OpenClawClient` — chat, stream, health, spend, logs, and `as_openai()` for drop-in OpenAI SDK compatibility.
- **[Interactive architecture diagram](docs/architecture.html)** — clickable flow visualization of the full request pipeline.

## Routing

| Model prefix | Backend | URL |
|---|---|---|
| `gpt-*` | OpenAI | `https://api.openai.com` |
| `claude-*` | Anthropic | `https://api.anthropic.com` |
| `gemini-*` | Google | `https://generativelanguage.googleapis.com` |
| `vllm/*` | vLLM | `http://localhost:8080` |
| `openclaw/*` | OpenClaw Gateway | `http://localhost:3000` |
| Everything else | Ollama | `http://localhost:11434` |

Edit `backends.json` to add, remove, or modify backends. All new fields are optional — existing configs work without changes:

```json
{
  "default": {
    "url": "http://localhost:11434",
    "name": "ollama",
    "timeout_s": 60
  },
  "routes": {
    "gpt-": {
      "url": "https://api.openai.com",
      "name": "openai",
      "timeout_s": 30,
      "pricing": {"prompt": 0.03, "completion": 0.06},
      "monthly_budget_usd": 100.0,
      "fallback": ["anthropic", "ollama"]
    }
  }
}
```

### Backend Config Fields

| Field | Required | Description |
|---|---|---|
| `url` | Yes | Backend base URL |
| `name` | Yes | Backend identifier |
| `timeout_s` | No | Request timeout in seconds (default 30) |
| `urls` | No | Multiple URLs for load balancing (overrides `url`) |
| `strategy` | No | Load balancing strategy: `round_robin`, `random`, `least_latency` |
| `fallback` | No | Ordered list of backend names to try on failure |
| `pricing` | No | `{"prompt": cost_per_1k, "completion": cost_per_1k}` for spend tracking |
| `monthly_budget_usd` | No | Monthly spend cap — returns 402 when exceeded |
| `api_key` | No | Backend-specific API key (proxy injects it, clients don't need it) |

## Quick Start

**Docker (recommended):**
```bash
PROXY_API_KEY=your-key docker compose up -d
docker compose exec ollama ollama pull llama3.2:1b
```

**Local:**
```bash
pip install -r requirements.txt
PROXY_API_KEY=your-key uvicorn proxy.main:app --host 0.0.0.0 --port 8005
```

**With security enforcement:**
```bash
PROXY_API_KEY=your-key SECURITY_PII_MODE=redact SECURITY_INJECTION_MODE=block docker compose up -d
```

## Usage

**Chat completion (routed to Ollama):**
```bash
curl http://localhost:8005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-key" \
  -d '{"model": "llama3.2:1b", "messages": [{"role": "user", "content": "Say hello"}]}'
```

**Streaming:**
```bash
curl -N http://localhost:8005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-key" \
  -d '{"model": "llama3.2:1b", "stream": true, "messages": [{"role": "user", "content": "Say hello"}]}'
```

**Health check (no auth required):**
```bash
curl http://localhost:8005/health
```

**View logs:**
```bash
curl -H "Authorization: Bearer your-secret-key" \
  "http://localhost:8005/logs?backend=ollama&limit=10&since=2026-04-25"
```

**Spend tracking:**
```bash
curl -H "Authorization: Bearer your-secret-key" http://localhost:8005/spend
```

**Live dashboard (no auth needed):**
```
http://localhost:8005/dashboard
```

**Python SDK:**
```python
from sdk import OpenClawClient

client = OpenClawClient(base_url="http://localhost:8005", api_key="your-key")

# Chat
response = client.chat("gpt-4", "What is 2+2?")
print(response.content)

# Streaming
for chunk in client.stream("llama3.2:1b", "Count to 5"):
    print(chunk, end="")

# Works with OpenAI SDK too
openai = client.as_openai()
resp = openai.chat.completions.create(model="gpt-4", messages=[...])
```

**Prometheus metrics:**
```
http://localhost:8005/metrics
```

## OpenClaw Integration

This proxy is designed to sit between OpenClaw and its LLM providers. Instead of configuring each provider separately in OpenClaw, point it at the proxy and let the router handle the rest.

**1. Start the proxy:**
```bash
PROXY_API_KEY=your-secret-key uvicorn proxy.main:app --host 0.0.0.0 --port 8005
```

**2. Configure OpenClaw to use the proxy as its LLM endpoint:**
```json
{
  "llm": {
    "provider": "openai-compatible",
    "base_url": "http://localhost:8005/v1",
    "api_key": "your-secret-key",
    "model": "gpt-4"
  }
}
```

Change the `model` field to route to any backend:
- `"model": "gpt-4"` — routes to OpenAI
- `"model": "claude-3-opus"` — routes to Anthropic
- `"model": "gemini-pro"` — routes to Google
- `"model": "llama3.2:1b"` — routes to Ollama (local)
- `"model": "openclaw/agent"` — routes to OpenClaw's own gateway

See `openclaw-config.example.json` for a full example with all provider options.

**What OpenClaw gets from the proxy:**
- Single endpoint for all LLM providers (no per-provider config)
- Auth, rate limiting, and size limits protecting your API keys
- PII scanning on prompts and responses
- Full request logging with latency, token usage, and security flags
- Streaming support for real-time agent output
- Automatic fallback to alternate providers if primary fails
- Response caching to reduce costs on repeated queries
- Spend tracking and budget alerts per backend

## Multi-Tenancy

For enterprises with multiple teams, create `tenants.json` (see `tenants.example.json`):

```json
{
  "tenants": {
    "team-alpha-key-xxx": {
      "name": "Team Alpha",
      "rate_limit_rpm": 100,
      "monthly_budget_usd": 500.0,
      "allowed_backends": ["openai", "anthropic", "ollama"],
      "allowed_models": ["gpt-4", "claude-3-opus", "llama3.2:1b"]
    },
    "team-beta-key-yyy": {
      "name": "Team Beta",
      "rate_limit_rpm": 30,
      "monthly_budget_usd": 50.0,
      "allowed_backends": ["ollama"]
    }
  }
}
```

Each team gets their own API key, rate limit, budget, and backend/model restrictions. When `tenants.json` exists, it overrides the single `PROXY_API_KEY`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PROXY_API_KEY` | _(empty, auth disabled)_ | Bearer token for authenticating requests |
| `RATE_LIMIT_RPM` | `60` | Max requests per minute per IP (0 = disabled) |
| `MAX_REQUEST_SIZE_MB` | `10` | Max request body size in MB |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Default Ollama backend URL |
| `PROXY_HOST` | `0.0.0.0` | Proxy listen host |
| `PROXY_PORT` | `8000` | Proxy listen port |
| `LOG_DIR` | `./logs` | Directory for JSONL log files |
| `CACHE_TTL_S` | `3600` | Cache entry time-to-live in seconds |
| `CACHE_MAX_ENTRIES` | `1000` | Maximum cached responses in memory |
| `SECURITY_PII_MODE` | `log` | `log`, `block` (403), or `redact` (auto-scrub PII) |
| `SECURITY_INJECTION_MODE` | `log` | `log` or `block` (403 on injection attempts) |
| `SECURITY_WEBHOOK_URL` | _(empty)_ | Webhook URL for security alerts (Slack, PagerDuty) |
| `LOG_REDACT_BODIES` | `false` | Exclude request/response bodies from JSONL logs |
| `REDIS_URL` | _(empty)_ | Redis URL for shared cache + rate limiting |
| `REDIS_CLUSTER` | `false` | Use Redis Cluster client for ~1M+ req/s |
| `DATABASE_URL` | _(empty)_ | PostgreSQL URL for persistent logs + spend |
| `DATABASE_READ_URL` | _(empty)_ | PG read replica URL for dashboard queries |
| `KAFKA_BOOTSTRAP_SERVERS` | _(empty)_ | Kafka brokers for async log writes (~500K+ req/s) |
| `KAFKA_TOPIC` | `llmproxy.requests` | Kafka topic for log events |

## Deployment

### Docker Compose (recommended)

```bash
# Standard (proxy + Ollama + Redis + PostgreSQL + Kafka + monitor)
PROXY_API_KEY=your-key docker compose up -d

# Extreme scale (Redis Cluster + PG read replica)
PROXY_API_KEY=your-key REDIS_CLUSTER=true \
  DATABASE_READ_URL=postgresql://user:pass@replica:5432/openclaw \
  docker compose up -d
```

Starts 6 containers: proxy, Ollama, Redis, PostgreSQL, Kafka, and a background monitor.

### Kubernetes (Helm)

```bash
helm install openclaw ./helm/openclaw-proxy \
  --set proxy.apiKey=your-key \
  --set proxy.securityPiiMode=redact
```

Prometheus auto-discovers the proxy via pod annotations — no scrape config needed.

### Kubernetes (manifests)

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml        # edit with real keys first
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml     # 3 replicas, health probes
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml        # TLS + SSE support
kubectl apply -f k8s/hpa.yaml            # auto-scale 2→10 pods
```

### VPS (one-command setup)

SSH into a fresh Ubuntu VPS as root and run:

```bash
curl -sSL https://raw.githubusercontent.com/ParthaMehtaOrg/openclaw-llmproxy/main/scripts/vps_setup.sh | bash
```

With HTTPS + Tailscale VPN:

```bash
curl -sSL https://raw.githubusercontent.com/ParthaMehtaOrg/openclaw-llmproxy/main/scripts/vps_setup.sh | \
  bash -s -- --domain llmproxy.example.com --tailscale-key tskey-auth-xxx
```

This automates 15 phases: system updates, non-root user, SSH hardening (port 2222, key-only), UFW firewall, Fail2Ban, auto security updates, Tailscale VPN (optional), IPv6 disabled, Docker install, proxy deployment (all 6 containers), SSL via Caddy (optional), systemd watchdog, and smoke tests.

## CI/CD & Monitoring

**GitHub Actions** runs on every push:
- 97 unit tests
- Docker build + integration checks
- Automated smoke tests

**Smoke test** (post-deploy verification):
```bash
bash scripts/smoke_test.sh http://localhost:8005 your-key
```

**Continuous monitor** (runs automatically as Docker sidecar):
```bash
docker compose logs -f monitor
```

Or standalone:
```bash
python scripts/monitor.py --url http://localhost:8005 --api-key your-key --interval 60
```

## Architecture

See the [interactive architecture diagram](docs/architecture.html) for a visual walkthrough.

### Request Flow
```
Client → Nginx/K8s Ingress (TLS)
  → Size Limit → Auth → Rate Limit (Redis) → Cache Check (Redis)
      ↓ (miss)
  Budget Check → API Translation → FastAPI Proxy → Load Balancer → Backend
      ↓               ↕                ↕              ↕           ↓ (fail)
   Spend (PG)     Key Inject       Security        Logger      Fallback Chain
                                   Scanner       (Kafka→PG→JSONL)
```

### Infrastructure Tiers
```
┌─────────────────────────────────────────────────────────────┐
│ Dev (zero config)          ~200 req/s                       │
│   uvicorn + in-memory state + JSONL logs                    │
├─────────────────────────────────────────────────────────────┤
│ Production                 ~5K-15K req/s                    │
│   + Redis (shared cache + rate limiting)                    │
│   + PostgreSQL (persistent logs + spend)                    │
├─────────────────────────────────────────────────────────────┤
│ High Scale                 ~50K-100K req/s                  │
│   + K8s HPA (2→10 pods)                                    │
│   + Multiple uvicorn workers                                │
├─────────────────────────────────────────────────────────────┤
│ Extreme Scale              ~500K-1M+ req/s                  │
│   + Kafka (async non-blocking log writes)                   │
│   + Redis Cluster (sharded cache/rate limiting)             │
│   + PG Read Replicas (dashboard queries off primary)        │
└─────────────────────────────────────────────────────────────┘
```

## Tests

```bash
python -m pytest tests/ -v    # 113 tests
```

## Project Structure

```
├── backends.json              # Backend routing config
├── proxy/
│   ├── main.py                # FastAPI app, proxy handler, streaming
│   ├── auth.py                # Bearer token authentication middleware
│   ├── ratelimit.py           # Sliding window rate limiter middleware
│   ├── sizelimit.py           # Request body size limit middleware
│   ├── retry.py               # Retry logic with exponential backoff
│   ├── router.py              # Model-prefix backend routing
│   ├── dashboard.py           # GET /logs and GET /spend endpoints
│   ├── web_dashboard.py       # GET /dashboard live HTML dashboard
│   ├── loadbalancer.py        # Round-robin, random, least-latency balancing
│   ├── fallback.py            # Model fallback chain resolution
│   ├── cache.py               # In-memory LRU response cache with TTL
│   ├── spend.py               # Cost tracking, budget enforcement
│   ├── logger.py              # JSONL request logging
│   ├── security.py            # PII & injection scanning (block/redact/log)
│   ├── alerts.py              # Webhook alerts for security events
│   ├── keymanager.py          # Per-backend API key injection
│   ├── metrics.py             # Prometheus /metrics endpoint
│   ├── tenants.py             # Multi-tenancy (per-team keys/limits)
│   ├── tracing.py             # X-Request-Id correlation IDs
│   ├── translators/           # API format translation
│   │   ├── __init__.py        # Translation dispatcher
│   │   ├── anthropic.py       # OpenAI ↔ Anthropic Messages API
│   │   └── gemini.py          # OpenAI ↔ Google Gemini API
│   └── config.py              # Environment variable config
├── sdk/                       # Python SDK
│   ├── __init__.py
│   └── client.py              # OpenClawClient (chat, stream, as_openai)
├── openclaw-config.example.json # Example OpenClaw config
├── tenants.example.json       # Example multi-tenant config
├── tests/                     # 113 tests
│   ├── test_proxy.py          # Core proxy tests (33)
│   ├── test_loadbalancer.py   # Load balancing tests (6)
│   ├── test_fallback.py       # Fallback chain tests (9)
│   ├── test_cache.py          # Response cache tests (11)
│   ├── test_spend.py          # Spend tracking tests (5)
│   ├── test_dashboard_web.py  # Web dashboard tests (4)
│   ├── test_translators.py    # API translation tests (21)
│   ├── test_enforcement.py    # Security enforcement tests (8)
│   └── test_enterprise.py     # Metrics, tenants, tracing, SDK, Helm (16)
├── scripts/
│   ├── smoke_test.sh          # Post-deploy verification (9 checks)
│   ├── monitor.py             # Continuous background health monitor
│   └── vps_setup.sh           # One-command VPS deploy (15 phases)
├── Dockerfile                 # Python 3.12-slim container
├── docker-compose.yml         # Full stack (proxy+Ollama+Redis+PG+Kafka+monitor)
├── backends.docker.json       # Docker-specific backend URLs
├── helm/openclaw-proxy/       # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/             # Deployment, Service, Secret, HPA
├── k8s/                       # Raw Kubernetes manifests
│   ├── namespace.yaml
│   ├── secret.yaml
│   ├── configmap.yaml
│   ├── deployment.yaml        # 3 replicas, health probes
│   ├── service.yaml
│   ├── ingress.yaml           # TLS + SSE support
│   └── hpa.yaml               # Auto-scale 2→10 pods
├── .github/workflows/ci.yml   # GitHub Actions CI/CD
├── systemd/
│   ├── openclaw-proxy.service
│   ├── openclaw-monitor.service
│   └── ollama.service
├── nginx/
│   └── openclaw-proxy.conf
├── docs/
│   └── architecture.html      # Interactive architecture diagram
└── requirements.txt
```
