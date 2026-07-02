#!/usr/bin/env bash
# test-egress-redaction.sh
# Validate that the egress-proxy scrubs secrets from outbound requests before
# they leave the cluster, by driving real HTTP calls FROM a running agent
# sandbox pod against httpbin.org (a public echo service allowlisted for this
# purpose — see apps/base/egress-proxy/networkpolicy.yaml and
# addon-configmap.yaml). httpbin echoes back exactly what it received, so a
# response missing the raw secret proves the proxy redacted it before
# forwarding — not just that some client-side masking happened.
#
# All test secrets are fake/synthetic. Only GET/HEAD is exercised, since the
# proxy enforces GET/HEAD-only for external destinations — PEM private-key
# and POST-body scrubbing cannot be exercised this way (see egress-proxy/README.md).
#
# Usage:
#   bash scripts/test-egress-redaction.sh
#
# Override context, namespace, pod selector, or container:
#   KUBE_CONTEXT=kind-vicegerent AGENT_LABEL=hermes bash scripts/test-egress-redaction.sh
set -uo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"
NAMESPACE="${NAMESPACE:-agent-sandbox}"
AGENT_LABEL="${AGENT_LABEL:-hermes}"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 1; }

current_ctx="$(kubectl config current-context 2>/dev/null || true)"
[[ "$current_ctx" == "$KUBE_CONTEXT" ]] || {
  echo "ERROR: current kubectl context is '${current_ctx:-<none>}', expected '$KUBE_CONTEXT'. Run: kubectl config use-context $KUBE_CONTEXT" >&2
  exit 1
}
CTX=(--context "$KUBE_CONTEXT")

POD="${POD:-$(kubectl "${CTX[@]}" -n "$NAMESPACE" get pods -l "vicegerent.io/dashboard=${AGENT_LABEL}" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)}"
[[ -n "$POD" ]] || {
  echo "ERROR: no running pod found in namespace '${NAMESPACE}' with label vicegerent.io/dashboard=${AGENT_LABEL}." >&2
  echo "  Is the ${AGENT_LABEL} sandbox up? kubectl -n ${NAMESPACE} get pods" >&2
  exit 1
}
CONTAINER="${CONTAINER:-$AGENT_LABEL}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
PASS=0; FAIL=0

pass() { echo -e "  ${GREEN}✓ $*${NC}"; ((PASS++)); }
fail() { echo -e "  ${RED}✗ $*${NC}"; ((FAIL++)); }
section() { echo -e "\n${CYAN}${BOLD}--- $* ---${NC}"; }

# ── exec-curl helper ─────────────────────────────────────────────────────────
# Runs curl inside the sandbox pod so the request goes through the real
# http_proxy/https_proxy env vars and trusted egress-proxy CA baked into the
# container — not a laptop-side shortcut that would bypass the Cilium policy
# keyed on the agent-sandbox namespace.
DELIM="===EGRESS_TEST_STATUS==="
pod_curl() {
  local url="$1"; shift
  kubectl "${CTX[@]}" -n "$NAMESPACE" exec "$POD" -c "$CONTAINER" -- \
    curl -sS --max-time 15 -o - -w "${DELIM}%{http_code}" "$@" "$url" 2>/dev/null
}

# Splits pod_curl's combined output into BODY and STATUS globals.
BODY=""; STATUS=""
run() {
  local raw; raw="$(pod_curl "$@")"
  STATUS="${raw##*"$DELIM"}"
  BODY="${raw%"$DELIM$STATUS"}"
}

echo ""
echo -e "${CYAN}${BOLD}=== Egress Proxy Redaction Test Suite ===${NC}"
echo -e "${CYAN}Pod: ${NAMESPACE}/${POD} (container: ${CONTAINER})${NC}"

# ── Section 1: baseline reachability ────────────────────────────────────────

section "1. httpbin.org reachable through the egress proxy"

echo -e "  ${YELLOW}probing GET https://httpbin.org/get ...${NC}"
run "https://httpbin.org/get"
if [[ "$STATUS" == "200" ]]; then
  pass "GET https://httpbin.org/get -> 200 (FQDN allowlist + proxy path OK)"
else
  fail "GET https://httpbin.org/get -> ${STATUS} (expected 200)"
  echo "    body: ${BODY:0:200}"
fi

# ── Section 2: header secret redaction ──────────────────────────────────────
# httpbin.org/headers echoes back exactly the headers it received, as JSON.

section "2. secrets in request headers are redacted before forwarding"

SLACK_BOT_TOKEN="xoxb-1234567890-abcdefghijklmnopqrstuvwx"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Slack bot token in custom header ...${NC}"
run "https://httpbin.org/headers" -H "X-Test-Secret: ${SLACK_BOT_TOKEN}"
if [[ "$BODY" == *"$SLACK_BOT_TOKEN"* ]]; then
  fail "Slack bot token reached httpbin unredacted"
elif [[ "$BODY" == *'[REDACTED]'* ]]; then
  pass "Slack bot token (xoxb-...) redacted in custom header"
else
  fail "Slack bot token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

SLACK_APP_TOKEN="xapp-1-A012345-6789012345-abcdefghijklmnop"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Slack app-level token in custom header ...${NC}"
run "https://httpbin.org/headers" -H "X-Test-Secret: ${SLACK_APP_TOKEN}"
if [[ "$BODY" == *"$SLACK_APP_TOKEN"* ]]; then
  fail "Slack app-level token reached httpbin unredacted"
elif [[ "$BODY" == *'[REDACTED]'* ]]; then
  pass "Slack app-level token (xapp-...) redacted in custom header"
else
  fail "Slack app-level token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BEARER_SECRET="sk-fake-bearer-secret-0000000000000000"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Authorization: Bearer header ...${NC}"
run "https://httpbin.org/headers" -H "Authorization: Bearer ${BEARER_SECRET}"
if [[ "$BODY" == *"$BEARER_SECRET"* ]]; then
  fail "Bearer token reached httpbin unredacted"
elif [[ "$BODY" == *'Bearer [REDACTED]'* ]]; then
  pass "Authorization: Bearer token redacted"
else
  fail "Bearer token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BASIC_CREDS="dGVzdHVzZXI6dGVzdHBhc3N3b3Jk" # base64("testuser:testpassword") — fake
echo -e "  ${YELLOW}probing Authorization: Basic header ...${NC}"
run "https://httpbin.org/headers" -H "Authorization: Basic ${BASIC_CREDS}"
if [[ "$BODY" == *"$BASIC_CREDS"* ]]; then
  fail "Basic auth credentials reached httpbin unredacted"
elif [[ "$BODY" == *'Basic [REDACTED]'* ]]; then
  pass "Authorization: Basic credentials redacted"
else
  fail "Basic auth credentials neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

API_KEY_SECRET="test-fake-api-key-0000000000000000"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing x-api-key header ...${NC}"
run "https://httpbin.org/headers" -H "x-api-key: ${API_KEY_SECRET}"
if [[ "$BODY" == *"$API_KEY_SECRET"* ]]; then
  fail "x-api-key header reached httpbin unredacted"
elif [[ "$BODY" == *'[REDACTED]'* ]]; then
  pass "x-api-key header redacted"
else
  fail "x-api-key header neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# ── Section 3: URL path/query redaction ─────────────────────────────────────
# The proxy scrubs flow.request.path (path + query string) before forwarding,
# so httpbin.org/get's echoed "url"/"args" fields reflect the redacted value.

section "3. secrets in the request URL query string are redacted"

QUERY_TOKEN="xoxb-9999999999-fakequerytokenvalue"
echo -e "  ${YELLOW}probing Slack token in query string ...${NC}"
run "https://httpbin.org/get?token=${QUERY_TOKEN}"
if [[ "$BODY" == *"$QUERY_TOKEN"* ]]; then
  fail "Slack token in query string reached httpbin unredacted"
elif [[ "$BODY" == *'[REDACTED]'* ]]; then
  pass "Slack token in query string redacted before forwarding"
else
  fail "Query-string token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# ── Section 4: negative controls — enforcement still active on the new host ─
# Guards against a too-broad allowlist entry accidentally opening up more
# than GET/HEAD, or the FQDN allowlist being effectively a wildcard.

section "4. method + FQDN enforcement unaffected by the httpbin allowlist entry"

echo -e "  ${YELLOW}probing POST https://httpbin.org/post — expect blocked ...${NC}"
run "https://httpbin.org/post" -X POST -d 'x=1'
if [[ "$STATUS" == "403" ]]; then
  pass "POST https://httpbin.org/post -> 403 (external POST still blocked)"
else
  fail "POST https://httpbin.org/post -> ${STATUS} (expected 403 — method enforcement regression?)"
  echo "    body: ${BODY:0:200}"
fi

echo -e "  ${YELLOW}probing GET https://example.com/ — expect blocked ...${NC}"
run "https://example.com/"
if [[ "$STATUS" == "403" ]]; then
  pass "GET https://example.com/ -> 403 (non-allowlisted FQDN still blocked)"
else
  fail "GET https://example.com/ -> ${STATUS} (expected 403 — is the allowlist a wildcard?)"
  echo "    body: ${BODY:0:200}"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}${BOLD}=== Summary ===${NC}"
echo -e "  ${GREEN}PASS: ${PASS}${NC}  ${RED}FAIL: ${FAIL}${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo "Diagnostics:"
  echo "  kubectl --context ${KUBE_CONTEXT} logs -n egress-proxy deploy/egress-proxy --tail=50"
  echo "  kubectl --context ${KUBE_CONTEXT} -n egress-proxy get ciliumnetworkpolicy egress-proxy -o yaml"
  echo "  kubectl --context ${KUBE_CONTEXT} -n egress-proxy get configmap egress-proxy-addon -o yaml"
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
