import os

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8000"))
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))
MAX_REQUEST_SIZE_MB = float(os.getenv("MAX_REQUEST_SIZE_MB", "10"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "3600"))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "1000"))

# Security enforcement mode: "log" (default), "block", or "redact"
SECURITY_PII_MODE = os.getenv("SECURITY_PII_MODE", "log")
SECURITY_INJECTION_MODE = os.getenv("SECURITY_INJECTION_MODE", "log")

# Webhook URL for security alerts (empty = disabled)
SECURITY_WEBHOOK_URL = os.getenv("SECURITY_WEBHOOK_URL", "")

# Log redaction: if true, don't log full request/response bodies
LOG_REDACT_BODIES = os.getenv("LOG_REDACT_BODIES", "false").lower() == "true"

# Scalability: Redis + PostgreSQL (empty = use in-memory fallback)
REDIS_URL = os.getenv("REDIS_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
