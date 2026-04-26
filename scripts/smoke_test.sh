#!/usr/bin/env bash
# Automated smoke test for OpenClaw LLM Proxy
# Run after deployment: ./scripts/smoke_test.sh [base_url] [api_key]
#
# Exits 0 if all checks pass, 1 if any fail.

set -euo pipefail

BASE_URL="${1:-http://localhost:8005}"
API_KEY="${2:-}"
PASS=0
FAIL=0

green() { printf "\033[32m✓ %s\033[0m\n" "$1"; }
red()   { printf "\033[31m✗ %s\033[0m\n" "$1"; }

check() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    if [ "$actual" = "$expected" ]; then
        green "$name (HTTP $actual)"
        PASS=$((PASS + 1))
    else
        red "$name — expected HTTP $expected, got HTTP $actual"
        FAIL=$((FAIL + 1))
    fi
}

auth_header=""
if [ -n "$API_KEY" ]; then
    auth_header="-H Authorization:Bearer $API_KEY"
fi

echo "══════════════════════════════════════════════"
echo "  OpenClaw LLM Proxy — Smoke Test"
echo "  Target: $BASE_URL"
echo "══════════════════════════════════════════════"
echo ""

# 1. Health check
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
check "Health endpoint" "200" "$STATUS"

# 2. Dashboard accessible
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/dashboard")
check "Dashboard page" "200" "$STATUS"

# 3. Dashboard metrics
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/dashboard/metrics")
check "Dashboard metrics API" "200" "$STATUS"

# 4. Auth — no token rejected
if [ -n "$API_KEY" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model": "test", "messages": [{"role": "user", "content": "hi"}]}')
    check "Auth rejects missing token" "401" "$STATUS"
fi

# 5. Auth — wrong token rejected
if [ -n "$API_KEY" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer wrong-token" \
        -d '{"model": "test", "messages": [{"role": "user", "content": "hi"}]}')
    check "Auth rejects wrong token" "401" "$STATUS"
fi

# 6. Spend endpoint
if [ -n "$API_KEY" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $API_KEY" "$BASE_URL/spend")
    check "Spend endpoint" "200" "$STATUS"
fi

# 7. Logs endpoint
if [ -n "$API_KEY" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $API_KEY" "$BASE_URL/logs?limit=1")
    check "Logs endpoint" "200" "$STATUS"
fi

# 8. Size limit — oversized payload rejected
if [ -n "$API_KEY" ]; then
    PAYLOAD=$(python3 -c "import json; print(json.dumps({'model':'test','messages':[{'role':'user','content':'x'*11000000}]}))")
    STATUS=$(echo "$PAYLOAD" | curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $API_KEY" \
        -d @-)
    check "Size limit rejects >10MB" "413" "$STATUS"
fi

# 9. Chat completion (only if Ollama is reachable)
if [ -n "$API_KEY" ]; then
    HEALTH=$(curl -s "$BASE_URL/health")
    if echo "$HEALTH" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin)['backends'].get('ollama',{}).get('status')=='reachable' else 1)" 2>/dev/null; then
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "$BASE_URL/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $API_KEY" \
            -d '{"model": "llama3.2:1b", "messages": [{"role": "user", "content": "Say ok"}]}')
        check "Chat completion via Ollama" "200" "$STATUS"
    else
        echo "  ⏭ Skipping chat test — Ollama not reachable"
    fi
fi

echo ""
echo "══════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
echo "══════════════════════════════════════════════"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
