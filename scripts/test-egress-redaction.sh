#!/usr/bin/env bash
# test-egress-redaction.sh
# Validate that the egress-proxy scrubs secrets from outbound requests before
# they leave the cluster, by driving real HTTP calls FROM a running agent
# sandbox pod against httpbin.io (a public echo service allowlisted for this
# purpose — see charts/egress-proxy/templates/networkpolicy.yaml and
# addon-configmap.yaml). httpbin echoes back exactly what it received, so a
# response missing the raw secret proves the proxy redacted it before
# forwarding — not just that some client-side masking happened.
#
# All test secrets are fake/synthetic. Only GET/HEAD is exercised, since the
# proxy enforces GET/HEAD-only for external destinations — PEM private-key
# and POST-body scrubbing cannot be exercised this way (see charts/egress-proxy/README.md).
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

section "1. httpbin.io reachable through the egress proxy"

echo -e "  ${YELLOW}probing GET https://httpbin.io/get ...${NC}"
run "https://httpbin.io/get"
if [[ "$STATUS" == "200" ]]; then
  pass "GET https://httpbin.io/get -> 200 (FQDN allowlist + proxy path OK)"
else
  fail "GET https://httpbin.io/get -> ${STATUS} (expected 200)"
  echo "    body: ${BODY:0:200}"
fi

# ── Section 2: header secret redaction ──────────────────────────────────────
# httpbin.io/headers echoes back exactly the headers it received, as JSON.

section "2. secrets in request headers are redacted before forwarding"

SLACK_BOT_TOKEN="xoxb-1234567890-abcdefghijklmnopqrstuvwx"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Slack bot token in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SLACK_BOT_TOKEN}"
if [[ "$BODY" == *"$SLACK_BOT_TOKEN"* ]]; then
  fail "Slack bot token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack bot token (xoxb-...) redacted in custom header"
else
  fail "Slack bot token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

SLACK_APP_TOKEN="xapp-1-A012345-6789012345-abcdefghijklmnop"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Slack app-level token in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SLACK_APP_TOKEN}"
if [[ "$BODY" == *"$SLACK_APP_TOKEN"* ]]; then
  fail "Slack app-level token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack app-level token (xapp-...) redacted in custom header"
else
  fail "Slack app-level token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BEARER_SECRET="sk-fake-bearer-secret-0000000000000000"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing Authorization: Bearer header ...${NC}"
run "https://httpbin.io/headers" -H "Authorization: Bearer ${BEARER_SECRET}"
if [[ "$BODY" == *"$BEARER_SECRET"* ]]; then
  fail "Bearer token reached httpbin unredacted"
elif [[ "$BODY" == *'Bearer <masked>'* ]]; then
  pass "Authorization: Bearer token redacted"
else
  fail "Bearer token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BASIC_CREDS="dGVzdHVzZXI6dGVzdHBhc3N3b3Jk" # base64("testuser:testpassword") — fake
echo -e "  ${YELLOW}probing Authorization: Basic header ...${NC}"
run "https://httpbin.io/headers" -H "Authorization: Basic ${BASIC_CREDS}"
if [[ "$BODY" == *"$BASIC_CREDS"* ]]; then
  fail "Basic auth credentials reached httpbin unredacted"
elif [[ "$BODY" == *'Basic <masked>'* ]]; then
  pass "Authorization: Basic credentials redacted"
else
  fail "Basic auth credentials neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

API_KEY_SECRET="test-fake-api-key-0000000000000000"  # pragma: allowlist secret
echo -e "  ${YELLOW}probing x-api-key header ...${NC}"
run "https://httpbin.io/headers" -H "x-api-key: ${API_KEY_SECRET}"
if [[ "$BODY" == *"$API_KEY_SECRET"* ]]; then
  fail "x-api-key header reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "x-api-key header redacted"
else
  fail "x-api-key header neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# Expanded REDACT_PATTERNS registry — probe a couple of the shapes added when the
# proxy's regex list was brought to parity with mcp-cerbos-shim's. Fixtures are
# fake and assembled at runtime (no full literal token in the source) plus a
# pragma allowlist, matching the fake-fixture convention above.
AWS_KEY="AKIA$(printf 'Q%.0s' $(seq 16))"  # pragma: allowlist secret (fake AWS access key id)
echo -e "  ${YELLOW}probing AWS access key id in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${AWS_KEY}"
if [[ "$BODY" == *"$AWS_KEY"* ]]; then
  fail "AWS access key id reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "AWS access key id (AKIA…) redacted in custom header"
else
  fail "AWS access key id neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

GITHUB_TOKEN="ghp_$(printf 'g%.0s' $(seq 36))"  # pragma: allowlist secret (fake GitHub PAT)
echo -e "  ${YELLOW}probing GitHub token in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${GITHUB_TOKEN}"
if [[ "$BODY" == *"$GITHUB_TOKEN"* ]]; then
  fail "GitHub token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "GitHub token (ghp_…) redacted in custom header"
else
  fail "GitHub token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# gitleaks-ONLY catch: SendGrid API tokens (gitleaks rule "sendgrid-api-token")
# are NOT in the regex registry, so redacting one proves the gitleaks sidecar
# path works end-to-end — not just that regex redaction still works. Same shape
# the shim's own gitleaks test uses: "SG." + 22 + "." + 43.
SENDGRID_KEY="SG.$(printf 'a%.0s' $(seq 22)).$(printf 'b%.0s' $(seq 43))"  # pragma: allowlist secret (fake SendGrid key)
echo -e "  ${YELLOW}probing SendGrid token (gitleaks-only, not in regex registry) ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SENDGRID_KEY}"
if [[ "$BODY" == *"$SENDGRID_KEY"* ]]; then
  fail "SendGrid token reached httpbin unredacted — gitleaks sidecar not scrubbing?"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "SendGrid token (SG.…) redacted by the gitleaks sidecar layer"
else
  fail "SendGrid token neither present nor visibly redacted — is the gitleaks sidecar up?"
  echo "    body: ${BODY:0:300}"
fi

# PII patterns (SSN / credit card / US phone) added for parity with the
# agentgateway promptGuard PII builtins. Fake fixtures assembled at runtime.
SSN_FAKE="123-45-6789"  # pragma: allowlist secret (fake SSN)
echo -e "  ${YELLOW}probing US SSN in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SSN_FAKE}"
if [[ "$BODY" == *"$SSN_FAKE"* ]]; then
  fail "SSN reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "US SSN (NNN-NN-NNNN) redacted in custom header"
else
  fail "SSN neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

VISA_FAKE="4$(printf '1%.0s' $(seq 15))"  # pragma: allowlist secret (fake Visa card)
echo -e "  ${YELLOW}probing Visa card number in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${VISA_FAKE}"
if [[ "$BODY" == *"$VISA_FAKE"* ]]; then
  fail "Visa card number reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Visa card number (starts 4, 16 digits) redacted in custom header"
else
  fail "Visa card number neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

PHONE_FAKE="(555) 123-4567"  # pragma: allowlist secret (fake US phone)
echo -e "  ${YELLOW}probing US phone number in custom header ...${NC}"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${PHONE_FAKE}"
if [[ "$BODY" == *"$PHONE_FAKE"* ]]; then
  fail "US phone number reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "US phone number ((NNN) NNN-NNNN) redacted in custom header"
else
  fail "US phone number neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# ── Section 3: URL path/query redaction ─────────────────────────────────────
# The proxy scrubs flow.request.path (path + query string) before forwarding,
# so httpbin.io/get's echoed "url"/"args" fields reflect the redacted value.

section "3. secrets in the request URL query string are redacted"

QUERY_TOKEN="xoxb-9999999999-fakequerytokenvalue"
echo -e "  ${YELLOW}probing Slack token in query string ...${NC}"
run "https://httpbin.io/get?token=${QUERY_TOKEN}"
if [[ "$BODY" == *"$QUERY_TOKEN"* ]]; then
  fail "Slack token in query string reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack token in query string redacted before forwarding"
else
  fail "Query-string token neither present nor visibly redacted — unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# ── Section 4: negative controls — enforcement still active on the new host ─
# Guards against a too-broad allowlist entry accidentally opening up more
# than GET/HEAD, or the FQDN allowlist being effectively a wildcard.

section "4. method + FQDN enforcement unaffected by the httpbin allowlist entry"

echo -e "  ${YELLOW}probing POST https://httpbin.io/post — expect blocked ...${NC}"
run "https://httpbin.io/post" -X POST -d 'x=1'
if [[ "$STATUS" == "403" ]]; then
  pass "POST https://httpbin.io/post -> 403 (external POST still blocked)"
else
  fail "POST https://httpbin.io/post -> ${STATUS} (expected 403 — method enforcement regression?)"
  echo "    body: ${BODY:0:200}"
fi

# A non-allowlisted HTTPS host is blocked at the CONNECT stage (http_connect()
# hook), not inside a tunnel like the POST check above (request() hook) — there
# is no tunnel to carry an in-band 403 back for the original GET, so curl's
# `-w "%{http_code}"` reports 000 (transfer never completed) even though the
# proxy DID respond 403 to the CONNECT itself (confirmed via proxy logs: "BLOCKED
# connect-fqdn-not-allowlisted host=example.com", and via `curl -v` showing
# "CONNECT tunnel failed, response 403"). A real allowlist-wildcard regression
# would show up as 200 with a real response body, not 000 — so this still
# catches that failure mode.
echo -e "  ${YELLOW}probing GET https://example.com/ — expect blocked ...${NC}"
run "https://example.com/"
if [[ "$STATUS" == "403" || "$STATUS" == "000" ]]; then
  pass "GET https://example.com/ -> ${STATUS} (non-allowlisted FQDN still blocked)"
else
  fail "GET https://example.com/ -> ${STATUS} (expected 403 or 000 — is the allowlist a wildcard?)"
  echo "    body: ${BODY:0:200}"
fi

# ── Section 5: git-upload-pack exception is narrow, not a github.com POST bypass ──
# Guards against the exception widening into "any POST to github.com is fine".

section "5. git-upload-pack exception is narrow (other POSTs to github.com still blocked)"

echo -e "  ${YELLOW}probing POST https://github.com/ (wrong path/content-type) — expect blocked ...${NC}"
run "https://github.com/" -X POST -d 'x=1'
if [[ "$STATUS" == "403" ]]; then
  pass "POST https://github.com/ -> 403 (git-upload-pack exception does not widen to all POSTs)"
else
  fail "POST https://github.com/ -> ${STATUS} (expected 403 — git-upload-pack exception may be too broad)"
  echo "    body: ${BODY:0:200}"
fi

# ── Section 6: response-body redaction ──────────────────────────────────────
# Every other test above proves REQUEST-side scrubbing (the secret is in a header
# or URL we send, and the request hook redacts it before httpbin echoes it back).
# To isolate RESPONSE-side scrubbing we need a secret that originates server-side,
# NOT one we send in the clear — anything we send is request-scrubbed first.
#
# Mechanism: send the secret base64-encoded in the URL path to httpbin.io/base64,
# which DECODES it server-side and returns the raw secret in the RESPONSE body.
# The base64 blob matches no request-side pattern, so it passes through untouched;
# the decoded secret then only ever exists in the response, where the response()
# hook must scrub it. We use a Notion token (ntn_…) on purpose: it's caught by the
# regex registry but NOT by gitleaks' default ruleset, so even if gitleaks were to
# base64-decode-and-scan the request URL, it wouldn't pre-empt this — the redaction
# we observe is unambiguously the RESPONSE-side regex layer.
#
# Caveat (confirm on first live run): this depends on httpbin.io/base64 returning a
# text/* (or application/json) Content-Type — the response scrubber deliberately
# skips binary bodies. If httpbin serves this as application/octet-stream the
# secret will pass through and this test will fail on content-type grounds, not a
# real redaction regression.

section "6. secrets in the RESPONSE body are redacted (echo-attack guard)"

NOTION_TOKEN="ntn_$(printf 't%.0s' $(seq 24))"  # pragma: allowlist secret (fake Notion token)
NOTION_B64="$(printf '%s' "$NOTION_TOKEN" | base64 | tr '+/' '-_' | tr -d '=')"
echo -e "  ${YELLOW}probing server-originated secret via httpbin.io/base64 decode ...${NC}"
run "https://httpbin.io/base64/${NOTION_B64}"
if [[ "$BODY" == *"$NOTION_TOKEN"* ]]; then
  fail "Notion token survived in the response body — response-side scrubbing not applied (or /base64 served a non-text Content-Type)"
  echo "    body: ${BODY:0:300}"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Notion token in the decoded response body redacted before reaching the sandbox"
else
  fail "Response body neither carried the token nor a <masked> marker — unexpected /base64 response"
  echo "    status: ${STATUS} body: ${BODY:0:300}"
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
