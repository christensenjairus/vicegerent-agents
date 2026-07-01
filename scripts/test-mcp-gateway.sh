#!/usr/bin/env bash
# test-mcp-gateway.sh
# Probe all MCP endpoints that agentgateway is supposed to expose.
#
# Usage (run from your mac):
#   # Option A: port-forward first in another terminal:
#   #   kubectl -n agentgateway-system port-forward svc/agentgateway-proxy 8080:80
#   #   GATEWAY_URL=http://localhost:8080 bash scripts/test-mcp-gateway.sh
#
#   # Option B: minikube NodePort:
#   #   export GATEWAY_URL=$(minikube -p vicegerent service agentgateway-proxy -n agentgateway-system --url)
#   #   bash scripts/test-mcp-gateway.sh
#
# Override API key (default is the kustomize-generated literal "hermes"):
#   MYKEY=myval bash scripts/test-mcp-gateway.sh
set -uo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
API_KEY="${API_KEY:-hermes}"

# MCP endpoints — must match apps/vicegerent/agents/felix/config.yaml mcp_servers
# (the HTTP MCPs fronted by agentgateway HTTPRoutes).
MCPS=(
  "tavily:/mcp/tavily"
  "firecrawl:/mcp/firecrawl"
  "host:/mcp/host"
)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}'
LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# Send an MCP request, optionally threading a session ID.
# Prints response body. Sets global SESSION_ID from Mcp-Session-Id response header.
SESSION_ID=""
mcp_post() {
  local url="$1" payload="$2" session="${3:-}"
  local hdr; hdr=$(mktemp)
  local extra=(); [[ -n "$session" ]] && extra=(-H "Mcp-Session-Id: $session")
  local body
  body=$(curl -sf --max-time 15 \
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

http_code() {
  local url="$1" payload="$2" session="${3:-}"
  local extra=(); [[ -n "$session" ]] && extra=(-H "Mcp-Session-Id: $session")
  curl -o /dev/null -s --max-time 10 -w "%{http_code}" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    "${extra[@]+"${extra[@]}"}" \
    -X POST "$url" -d "$payload" 2>/dev/null || echo "000"
}

parse_tools() {
  python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split(chr(10)) if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    tools = d.get('result', {}).get('tools', [])
    print(chr(10).join(t['name'] for t in tools) if tools else '(no tools)')
except Exception as e:
    sys.stderr.write(f'parse error: {e}  raw: {body[:200]}' + chr(10))
    sys.exit(1)
"
}

echo ""
echo -e "${CYAN}=== agentgateway MCP endpoint test ===${NC}"
echo -e "${CYAN}Gateway: ${GATEWAY_URL}${NC}"
echo ""

for entry in "${MCPS[@]}"; do
  name="${entry%%:*}"
  path="${entry#*:}"
  url="${GATEWAY_URL}${path}"
  echo -e "${CYAN}--- ${name} (${path}) ---${NC}"

  SESSION_ID=""
  code=$(http_code "$url" "$INIT")

  case "$code" in
    000) echo -e "  ${RED}x UNREACHABLE${NC} — nothing at ${url}"; ((FAIL++)); continue ;;
    404) echo -e "  ${RED}x 404${NC} — HTTPRoute missing or backend not registered"
         echo    "    kubectl -n agentgateway-system get httproutes"
         ((FAIL++)); continue ;;
    421) echo -e "  ${RED}x 421 Misdirected Request${NC} — Python MCP SDK host-allowlist lock"
         echo    "    kubectl -n <mcp-ns> logs <pod> | grep -i 'Invalid Host'"
         ((FAIL++)); continue ;;
    [45]*) raw=$(mcp_post "$url" "$INIT" || true)
           echo -e "  ${RED}x HTTP ${code}${NC}: ${raw:0:300}"
           ((FAIL++)); continue ;;
  esac

  # Successful initialize — capture session ID from response headers
  mcp_post "$url" "$INIT" > /dev/null
  echo -e "  ${GREEN}+ reachable (HTTP ${code})${NC}"

  if [[ -z "$SESSION_ID" ]]; then
    echo -e "  ${YELLOW}? no Mcp-Session-Id in response — cannot send tools/list${NC}"
    ((WARN++)); continue
  fi

  resp=$(mcp_post "$url" "$LIST" "$SESSION_ID" 2>&1 || true)
  if [[ -z "$resp" ]]; then
    echo -e "  ${YELLOW}? tools/list returned empty${NC}"
    ((WARN++)); continue
  fi

  tools=$(echo "$resp" | parse_tools 2>/tmp/mcp-parse-err || echo "PARSE_ERROR")

  if [[ "$tools" == "PARSE_ERROR" ]]; then
    echo -e "  ${YELLOW}? tools/list unparseable${NC}"
    [[ -f /tmp/mcp-parse-err ]] && head -3 /tmp/mcp-parse-err | sed 's/^/    /'
    ((WARN++))
  elif [[ "$tools" == "(no tools)" ]]; then
    echo -e "  ${YELLOW}? 0 tools returned — allowlist blocking all?${NC}"
    echo    "    kubectl -n agentgateway-system get agentgatewaypolicies ${name}-policy -o yaml"
    ((WARN++))
  else
    count=$(echo "$tools" | wc -l | tr -d ' ')
    echo -e "  ${GREEN}+ ${count} tools:${NC}"
    echo "$tools" | sed 's/^/      /'
    ((PASS++))
  fi
  echo ""
done

echo ""
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "  ${GREEN}PASS: ${PASS}${NC}  ${YELLOW}WARN: ${WARN}${NC}  ${RED}FAIL: ${FAIL}${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo "Useful diagnostics:"
  echo "  kubectl -n agentgateway-system get httproutes,agentgatewaybackends,agentgatewaypolicies"
  echo "  kubectl -n agentgateway-system logs -l app.kubernetes.io/name=agentgateway-proxy --tail=50"
  echo "  kubectl -n agentgateway-system get events --sort-by=.lastTimestamp | tail -20"
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
