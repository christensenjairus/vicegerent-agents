#!/usr/bin/env bash
# test-mcp-policies.sh
# Validate that agentgateway + Cerbos policies are correctly enforced at runtime.
#
# Tests two layers:
#   1. agentgateway tool-name allowlist  — certain tools must NOT appear in tools/list
#   2. Cerbos Secret block              — kubernetes__getResource/listResources/describeResource
#                                         on a Secret must be denied at tools/call time
#
# Usage (port-forward in another terminal first):
#   kubectl -n agentgateway-system port-forward svc/agentgateway-proxy 8080:80
#   bash scripts/test-mcp-policies.sh
#
# Override gateway URL or API key:
#   GATEWAY_URL=http://localhost:8080 MY_KEY=hermes bash scripts/test-mcp-policies.sh
set -uo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
API_KEY="${MY_KEY:-hermes}"
# Random secret name so the test is self-describing in k8s audit logs
SECRET_NAME="policy-test-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom 2>/dev/null | head -c8 || echo 'xxxxxxxx')"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
PASS=0; FAIL=0

pass() { echo -e "  ${GREEN}✓ $*${NC}"; ((PASS++)); }
fail() { echo -e "  ${RED}✗ $*${NC}"; ((FAIL++)); }
section() { echo -e "\n${CYAN}${BOLD}--- $* ---${NC}"; }

# ── MCP session helpers ─────────────────────────────────────────────────────

SESSION_ID=""
mcp_post() {
  local url="$1" payload="$2" session="${3:-}"
  local hdr; hdr=$(mktemp)
  local extra=(); [[ -n "$session" ]] && extra=(-H "Mcp-Session-Id: $session")
  local body
  body=$(curl -sf --max-time 20 \
    -D "$hdr" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    "${extra[@]+"${extra[@]}"}" \
    -X POST "$url" -d "$payload" 2>/dev/null) || true
  SESSION_ID=$(grep -i '^mcp-session-id:' "$hdr" | awk '{print $2}' | tr -d '\r' || true)
  rm -f "$hdr"
  printf '%s' "$body"
}

mcp_http_code() {
  local url="$1" payload="$2" session="${3:-}"
  local extra=(); [[ -n "$session" ]] && extra=(-H "Mcp-Session-Id: $session")
  curl -o /dev/null -s --max-time 20 -w "%{http_code}" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    "${extra[@]+"${extra[@]}"}" \
    -X POST "$url" -d "$payload" 2>/dev/null || echo "000"
}

# Open a session to an MCP endpoint. Prints the session ID and sets SESSION_ID.
open_session() {
  local url="$1"
  local init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"policy-test","version":"1"}}}'
  SESSION_ID=""
  mcp_post "$url" "$init" >/dev/null
  if [[ -z "$SESSION_ID" ]]; then
    echo -e "  ${RED}FATAL: could not open MCP session to ${url}${NC}" >&2
    exit 1
  fi
}

# Fetch the tool list for the current SESSION_ID. Prints names one per line.
get_tools() {
  local url="$1"
  local list='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
  local resp; resp=$(mcp_post "$url" "$list" "$SESSION_ID")
  python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split('\n') if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    for t in d.get('result', {}).get('tools', []):
        print(t['name'])
except Exception as e:
    sys.stderr.write(f'parse error: {e}\n')
" <<< "$resp"
}

# Call a tool. Prints raw SSE/JSON response. Returns the HTTP status code via stdout of mcp_http_code.
call_tool() {
  local url="$1" tool="$2" args_json="$3"
  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':sys.argv[1],'arguments':json.loads(sys.argv[2])}}))
" "$tool" "$args_json")
  mcp_post "$url" "$payload" "$SESSION_ID"
}

call_tool_code() {
  local url="$1" tool="$2" args_json="$3"
  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':sys.argv[1],'arguments':json.loads(sys.argv[2])}}))
" "$tool" "$args_json")
  mcp_http_code "$url" "$payload" "$SESSION_ID"
}

# Parse "is this a Cerbos-denied response?" from a tools/call SSE response.
# Looks for the error text the shim returns.
# is_cerbos_denied: 'denied' only when agentgateway/Cerbos blocked the call.
# 'allowed' when Cerbos passed the call through (even if k8s then errored).
#
# Cerbos denials come back as JSON-RPC error code -32001 with the message
# "Access denied by security policy...". k8s-level errors (tool/config failures)
# use -32603. We key on the error code — it's stable and unambiguous.
is_cerbos_denied() {
  local resp="$1"
  echo "$resp" | python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split('\n') if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    err = d.get('error', {})
    code = err.get('code')
    msg  = str(err.get('message', ''))
    # -32001 is the agentgateway/Cerbos policy-denial code.
    # Also catch the human-readable phrase as a belt-and-suspenders fallback.
    if code == -32001 or 'Access denied by security policy' in msg:
        print('denied')
    else:
        print('allowed')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# ── Setup ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}${BOLD}=== MCP Policy Enforcement Test Suite ===${NC}"
echo -e "${CYAN}Gateway: ${GATEWAY_URL}${NC}"
echo -e "${CYAN}Secret probe name: ${SECRET_NAME}${NC}"

HOST_URL="${GATEWAY_URL}/mcp/host"
TAVILY_URL="${GATEWAY_URL}/mcp/tavily"
FIRECRAWL_URL="${GATEWAY_URL}/mcp/firecrawl"

# ── Section 1: agentgateway tool-name allowlist ───────────────────────────────

section "1. agentgateway tool allowlist — host backend"

open_session "$HOST_URL"
TOOLS=$(get_tools "$HOST_URL")

# Tools that MUST exist (spot check one per MCP server)
for must_have in "kubernetes__getResource" "linear__list_issues" "notion__notion-search"; do
  if echo "$TOOLS" | grep -qx "$must_have"; then
    pass "allowed tool present: ${must_have}"
  else
    fail "allowed tool MISSING: ${must_have} (policy too strict?)"
  fi
done

# Tools that must NOT exist — if mcp-proxy-server ever exposes an internal tool
# or a new server is added without a policy update, these would appear
# We verify the prefix gate: any tool NOT starting with notion__, linear__, kubernetes__
UNLISTED=$(echo "$TOOLS" | grep -vE '^(notion__|linear__|kubernetes__)' || true)
if [[ -z "$UNLISTED" ]]; then
  pass "no unlisted tool prefixes exposed on host backend"
else
  fail "unlisted tools exposed (policy gap):\n$(echo "$UNLISTED" | sed 's/^/      /')"
fi

section "2. agentgateway tool allowlist — tavily backend"

open_session "$TAVILY_URL"
TAVILY_TOOLS=$(get_tools "$TAVILY_URL")
ALLOWED_TAVILY="tavily_search tavily_extract tavily_map tavily_crawl"

# Must have exactly the allowed set
for tool in $ALLOWED_TAVILY; do
  if echo "$TAVILY_TOOLS" | grep -qx "$tool"; then
    pass "allowed tool present: ${tool}"
  else
    fail "allowed tool MISSING: ${tool}"
  fi
done

# Nothing outside the allowed set
EXTRA_TAVILY=$(echo "$TAVILY_TOOLS" | grep -vxF -f <(echo "$ALLOWED_TAVILY" | tr ' ' '\n') || true)
if [[ -z "$EXTRA_TAVILY" ]]; then
  pass "tavily exposes no unlisted tools"
else
  fail "tavily exposes unlisted tools (policy gap): ${EXTRA_TAVILY}"
fi

section "3. agentgateway tool allowlist — firecrawl backend"

open_session "$FIRECRAWL_URL"
FIRECRAWL_TOOLS=$(get_tools "$FIRECRAWL_URL")
ALLOWED_FIRECRAWL="firecrawl_scrape firecrawl_search firecrawl_map firecrawl_extract firecrawl_crawl firecrawl_check_crawl_status"

for tool in $ALLOWED_FIRECRAWL; do
  if echo "$FIRECRAWL_TOOLS" | grep -qx "$tool"; then
    pass "allowed tool present: ${tool}"
  else
    fail "allowed tool MISSING: ${tool}"
  fi
done

EXTRA_FIRECRAWL=$(echo "$FIRECRAWL_TOOLS" | grep -vxF -f <(echo "$ALLOWED_FIRECRAWL" | tr ' ' '\n') || true)
if [[ -z "$EXTRA_FIRECRAWL" ]]; then
  pass "firecrawl exposes no unlisted tools"
else
  fail "firecrawl exposes unlisted tools (policy gap): ${EXTRA_FIRECRAWL}"
fi

# ── Section 4: Cerbos Secret block ──────────────────────────────────────────
# All probes are READ-ONLY. Secret probes use a randomly generated name that
# almost certainly does not exist — even if policy fails, there is nothing to
# return. The non-secret control probe uses a namespace that certainly exists
# but we ask for a resource name that won't exist either, so Cerbos is tested
# without leaking real cluster state if a policy is mis-configured.

section "4. Cerbos guardrail — Secret reads must be denied"

open_session "$HOST_URL"

# 4a: getResource on a Secret — must be denied before k8s is ever contacted.
# Args: context (required by k8s-mcp-server), kind, name, namespace.
# The secret name is random and almost certainly absent; Cerbos denies before k8s lookup.
echo -e "  ${YELLOW}probing kubernetes__getResource(Secret/${SECRET_NAME}) ...${NC}"
RESP=$(call_tool "$HOST_URL" "kubernetes__getResource" \
  "{\"context\":\"uw1-prod1\",\"kind\":\"Secret\",\"name\":\"${SECRET_NAME}\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  pass "kubernetes__getResource(Secret) → denied by Cerbos"
elif [[ "$VERDICT" == "allowed" ]]; then
  fail "kubernetes__getResource(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
else
  fail "kubernetes__getResource(Secret) → unknown response"
  echo "    raw: ${RESP:0:300}"
fi

# 4b: listResources of Secrets — must be denied.
# Uses capital K 'Kind' as required by the listResources handler.
# Namespace is intentionally a fake one so even if policy fails, no secrets are returned.
echo -e "  ${YELLOW}probing kubernetes__listResources(Kind=Secret, ns=policy-test-ns) ...${NC}"
RESP=$(call_tool "$HOST_URL" "kubernetes__listResources" \
  "{\"context\":\"uw1-prod1\",\"Kind\":\"Secret\",\"namespace\":\"policy-test-nonexistent-ns\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  pass "kubernetes__listResources(Secret) → denied by Cerbos"
elif [[ "$VERDICT" == "allowed" ]]; then
  fail "kubernetes__listResources(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
else
  fail "kubernetes__listResources(Secret) → unknown response"
  echo "    raw: ${RESP:0:300}"
fi

# 4c: describeResource on a Secret — must be denied.
# Capital K 'Kind' as required by the describeResource handler.
echo -e "  ${YELLOW}probing kubernetes__describeResource(Kind=Secret/${SECRET_NAME}) ...${NC}"
RESP=$(call_tool "$HOST_URL" "kubernetes__describeResource" \
  "{\"context\":\"uw1-prod1\",\"Kind\":\"Secret\",\"name\":\"${SECRET_NAME}\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  pass "kubernetes__describeResource(Secret) → denied by Cerbos"
elif [[ "$VERDICT" == "allowed" ]]; then
  fail "kubernetes__describeResource(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
else
  fail "kubernetes__describeResource(Secret) → unknown response"
  echo "    raw: ${RESP:0:300}"
fi

# 4d: getResource on a ConfigMap — must NOT be denied (Cerbos should not over-block).
# Uses a name that won't exist so no real data is exposed even if somehow allowed.
# A k8s-level 404 is fine here — it means Cerbos passed it through (correct behaviour).
echo -e "  ${YELLOW}probing kubernetes__getResource(ConfigMap/policy-test-nonexistent) — expect allowed ...${NC}"
RESP=$(call_tool "$HOST_URL" "kubernetes__getResource" \
  "{\"context\":\"uw1-prod1\",\"kind\":\"ConfigMap\",\"name\":\"policy-test-nonexistent\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  fail "kubernetes__getResource(ConfigMap) → DENIED — Cerbos is over-blocking non-secrets!"
else
  pass "kubernetes__getResource(ConfigMap) → passed Cerbos (k8s-level result is irrelevant)"
fi

# 4e: listResources for Pods — must NOT be denied (non-secret, non-empty kind).
echo -e "  ${YELLOW}probing kubernetes__listResources(Kind=Pod) — expect allowed ...${NC}"
RESP=$(call_tool "$HOST_URL" "kubernetes__listResources" \
  "{\"context\":\"uw1-prod1\",\"Kind\":\"Pod\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  fail "kubernetes__listResources(Pod) → DENIED — Cerbos is over-blocking non-secrets!"
else
  pass "kubernetes__listResources(Pod) → passed Cerbos (correct)"
fi

# 4f: Guardrail attachment check — verify the policy carries the shim attachment.
# A missing guardrail silently fails open (FailClosed only covers shim failures, not absence).
echo -e "  ${YELLOW}verifying guardrail attached to host-mcp-tools policy ...${NC}"
if command -v kubectl &>/dev/null; then
  GUARDRAIL=$(kubectl -n agentgateway-system get agentgatewaypolicy host-mcp-tools \
    -o jsonpath='{.spec.backend.mcp.guardrails.processors[0].remote.backendRef.name}' 2>/dev/null || true)
  if [[ "$GUARDRAIL" == "mcp-cerbos-shim" ]]; then
    pass "guardrail attached: mcp-cerbos-shim (FailClosed)"
  elif [[ -z "$GUARDRAIL" ]]; then
    fail "guardrail NOT attached to host-mcp-tools — Secret block silently fails open!"
  else
    fail "guardrail attached to unexpected backend: ${GUARDRAIL}"
  fi
else
  echo -e "  ${YELLOW}~ kubectl not available — skipping live guardrail attachment check${NC}"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}${BOLD}=== Summary ===${NC}"
echo -e "  ${GREEN}PASS: ${PASS}${NC}  ${RED}FAIL: ${FAIL}${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo "Diagnostics:"
  echo "  kubectl -n agentgateway-system get agentgatewaypolicies -o yaml"
  echo "  kubectl -n agentgateway-system logs deploy/agentgateway-proxy --tail=50 | grep -i 'cerbos\|guardrail\|deny'"
  echo "  kubectl -n cerbos logs deploy/cerbos --tail=30"
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
