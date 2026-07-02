#!/usr/bin/env bash
# test-mcp-policies.sh
# Validate that agentgateway + Cerbos policies are correctly enforced at runtime.
#
# All backends are aggregated behind the single ToolHive vMCP (/mcp/vmcp); tools
#   surface prefixed by workload (e.g. kubernetes_resources_get) with no allowlist.
#   Enforced policy under test:
#     Cerbos Secret block — kubernetes_resources_get/kubernetes_resources_list on a
#                           Secret must be denied at tools/call time.
#
# Usage (port-forward in another terminal first):
#   kubectl -n agentgateway-system port-forward svc/agentgateway-proxy 8080:80
#   bash scripts/test-mcp-policies.sh
#
# Override gateway URL or API key:
#   GATEWAY_URL=http://localhost:8080 MY_KEY=hermes bash scripts/test-mcp-policies.sh
set -uo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"
if command -v kubectl >/dev/null 2>&1; then
  current_ctx="$(kubectl config current-context 2>/dev/null || true)"
  [[ "$current_ctx" == "$KUBE_CONTEXT" ]] || {
    echo "ERROR: current kubectl context is '${current_ctx:-<none>}', expected '$KUBE_CONTEXT'. Run: kubectl config use-context $KUBE_CONTEXT" >&2
    exit 1
  }
fi

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

VMCP_URL="${GATEWAY_URL}/mcp/vmcp"

# ── Section 1: vMCP tool surface ──────────────────────────────────────────────

section "1. vMCP tool surface — aggregated at the gateway"

open_session "$VMCP_URL"
TOOLS=$(get_tools "$VMCP_URL")

# Tool selection is done in the vMCP (aggregation.tools, per backend); agentgateway
# adds no per-tool allowlist in this setup (it could in a centralized one). Just
# confirm the vMCP is aggregating and exposing tools, prefixed by workload
# ({workload}_<tool>).
TOOL_COUNT=$(echo "$TOOLS" | grep -c . || true)
if [[ "$TOOL_COUNT" -gt 0 ]]; then
  pass "vMCP exposes ${TOOL_COUNT} aggregated tools"
else
  fail "vMCP exposed no tools — backends down or vMCP not aggregating?"
fi

# The Cerbos Secret block (section 2) depends on these exact kubernetes tool names.
for must_have in "kubernetes_resources_get" "kubernetes_resources_list"; do
  if echo "$TOOLS" | grep -qx "$must_have"; then
    pass "tool present: ${must_have}"
  else
    fail "tool MISSING from vMCP: ${must_have} (kubernetes backend down?)"
  fi
done

# ── Section 2: Cerbos Secret block ──────────────────────────────────────────
# All probes are READ-ONLY. Secret probes use a randomly generated name that
# almost certainly does not exist — even if policy fails, there is nothing to
# return. The non-secret control probe uses a namespace that certainly exists
# but we ask for a resource name that won't exist either, so Cerbos is tested
# without leaking real cluster state if a policy is mis-configured.

section "2. Cerbos guardrail — Secret reads must be denied"

open_session "$VMCP_URL"

# 3a: resources_get on a Secret — must be denied before k8s is ever contacted.
# Args: apiVersion + kind (kubernetes-mcp-server format). No context arg needed.
# The secret name is random and almost certainly absent; Cerbos denies before k8s lookup.
echo -e "  ${YELLOW}probing kubernetes_resources_get(Secret/${SECRET_NAME}) ...${NC}"
RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_get" \
  "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"name\":\"${SECRET_NAME}\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  pass "kubernetes_resources_get(Secret) → denied by Cerbos"
elif [[ "$VERDICT" == "allowed" ]]; then
  fail "kubernetes_resources_get(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
else
  fail "kubernetes_resources_get(Secret) → unknown response"
  echo "    raw: ${RESP:0:300}"
fi

# 3b: resources_list of Secrets — must be denied.
# Namespace is intentionally a fake one so even if policy fails, no secrets are returned.
echo -e "  ${YELLOW}probing kubernetes_resources_list(kind=Secret, ns=policy-test-ns) ...${NC}"
RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_list" \
  "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"namespace\":\"policy-test-nonexistent-ns\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  pass "kubernetes_resources_list(Secret) → denied by Cerbos"
elif [[ "$VERDICT" == "allowed" ]]; then
  fail "kubernetes_resources_list(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
else
  fail "kubernetes_resources_list(Secret) → unknown response"
  echo "    raw: ${RESP:0:300}"
fi

# 3c: resources_get on a ConfigMap — must NOT be denied (Cerbos should not over-block).
# A k8s-level 404 is fine — it means Cerbos passed it through (correct behaviour).
echo -e "  ${YELLOW}probing kubernetes_resources_get(ConfigMap/policy-test-nonexistent) — expect allowed ...${NC}"
RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_get" \
  "{\"apiVersion\":\"v1\",\"kind\":\"ConfigMap\",\"name\":\"policy-test-nonexistent\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  fail "kubernetes_resources_get(ConfigMap) → DENIED — Cerbos is over-blocking non-secrets!"
else
  pass "kubernetes_resources_get(ConfigMap) → passed Cerbos (k8s-level result is irrelevant)"
fi

# 3d: resources_list for Pods — must NOT be denied (non-secret, non-empty kind).
echo -e "  ${YELLOW}probing kubernetes_resources_list(kind=Pod) — expect allowed ...${NC}"
RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_list" \
  "{\"apiVersion\":\"v1\",\"kind\":\"Pod\",\"namespace\":\"default\"}")
VERDICT=$(is_cerbos_denied "$RESP")
if [[ "$VERDICT" == "denied" ]]; then
  fail "kubernetes_resources_list(Pod) → DENIED — Cerbos is over-blocking non-secrets!"
else
  pass "kubernetes_resources_list(Pod) → passed Cerbos (correct)"
fi

# 3e: Guardrail attachment check — verify the policy carries the shim attachment.
# A missing guardrail silently fails open (FailClosed only covers shim failures, not absence).
echo -e "  ${YELLOW}verifying guardrail attached to vmcp-mcp-tools policy ...${NC}"
if command -v kubectl &>/dev/null; then
  GUARDRAIL=$(kubectl --context "$KUBE_CONTEXT" -n agentgateway-system get agentgatewaypolicy vmcp-mcp-tools \
    -o jsonpath='{.spec.backend.mcp.guardrails.processors[0].remote.backendRef.name}' 2>/dev/null || true)
  if [[ "$GUARDRAIL" == "mcp-cerbos-shim" ]]; then
    pass "guardrail attached: mcp-cerbos-shim (FailClosed)"
  elif [[ -z "$GUARDRAIL" ]]; then
    fail "guardrail NOT attached to vmcp-mcp-tools — Secret block silently fails open!"
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
