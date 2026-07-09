#!/usr/bin/env bash
# test-mcp-gateway.sh
# Probe all MCP endpoints that agentgateway is supposed to expose.
#
# Usage (run from your mac):
#   # Port-forward first in another terminal:
#   #   kubectl -n agentgateway-system port-forward svc/agentgateway-proxy 8080:80
#   #   GATEWAY_URL=http://localhost:8080 bash scripts/test-mcp-gateway.sh [keyword]
#
# With no keyword, enumerate the reachable tools per backend. With a keyword,
# search find_tool for matching tools (e.g. 'kubernetes', 'context', 'notion').
#
# Override API key (default is the kustomize-generated literal "hermes"):
#   MYKEY=myval bash scripts/test-mcp-gateway.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
API_KEY="${API_KEY:-hermes}"
SERVERS_CONFIG="${SERVERS_CONFIG:-$SCRIPT_DIR/../host/mcp/toolhive-servers.json}"
# Optional free-form keyword: search find_tool for matching tools instead of
# enumerating every backend (e.g. 'kubernetes', 'context', 'notion').
QUERY="${1:-}"

# MCP endpoints — all backends are aggregated behind the single ToolHive vMCP,
# fronted by the agentgateway /mcp/vmcp HTTPRoute.
MCPS=(
  "vmcp:/mcp/vmcp"
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

# The vMCP tool-discovery optimizer collapses every backend tool behind two
# meta-tools (find_tool/call_tool), so tools/list can't enumerate the real tools.
# We probe find_tool instead. With no QUERY, search once per expected backend
# (keyword = the backend's own name) and union the hits, surfacing which backends
# are live and a representative set of each one's tools. With a QUERY, run that one
# search and show matches across all backends (e.g. 'kubernetes', 'context', 'notion').
# find_tool is a ranked BM25+semantic search capped at a handful of hits per query,
# so results are a reachable sample, not the full catalog. Enumerate mode exits 4
# if the index came back empty; search mode always exits 0 (the endpoint is healthy).
discover_tools() {
  local url="$1" session="$2" query="${3:-}"
  URL="$url" API_KEY="$API_KEY" SESSION="$session" SERVERS_CONFIG="$SERVERS_CONFIG" QUERY="$query" python3 -c "
import os, json, urllib.request
url, key, sid, cfg = os.environ['URL'], os.environ['API_KEY'], os.environ['SESSION'], os.environ['SERVERS_CONFIG']
query = os.environ['QUERY']
G, Y, N = chr(27)+'[0;32m', chr(27)+'[1;33m', chr(27)+'[0m'
servers = [s['name'] for s in json.load(open(cfg))['servers']]

def find(keyword):
    payload = json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
        'name':'find_tool','arguments':{'tool_description':keyword,'tool_keywords':[keyword]}}}).encode()
    req = urllib.request.Request(url, data=payload, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type':'application/json',
        'Accept':'application/json, text/event-stream', 'Mcp-Session-Id': sid})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
        data = [l[5:].strip() for l in raw.split(chr(10)) if l.startswith('data:')]
        d = json.loads(data[0] if data else raw)
        inner = json.loads(d['result']['content'][0]['text'])
        return inner.get('tools') or []
    except Exception:
        return []

def owner(name):
    return next((p for p in servers if name.startswith(p + '_')), '?')

if query:
    matches = {t['name']: t for t in find(query)}
    if not matches:
        print(f'  {Y}- no tools match \"{query}\"{N}')
        raise SystemExit(0)
    print(f'  {G}+ {len(matches)} tools match \"{query}\":{N}')
    for name in sorted(matches):
        desc = ' '.join((matches[name].get('description') or '').split())
        if len(desc) > 100: desc = desc[:99] + '…'
        print(f'      {name}  [{owner(name)}]')
        if desc: print(f'        {desc}')
    raise SystemExit(0)

union = {}
for s in servers:
    for t in find(s):
        union[t['name']] = owner(t['name'])
by_server = {s: sorted(n for n, o in union.items() if o == s) for s in servers}
live = [s for s in servers if by_server[s]]
for s in servers:
    tools = by_server[s]
    if tools:
        print(f'  {G}+ {s}: {len(tools)} tools reachable{N}')
        for t in tools:
            print(f'      {t}')
    else:
        print(f'  {Y}- {s}: none reachable (disabled or backend down){N}')
total = len(union)
print(f'  {G}{total} tools reachable across {len(live)} backend(s): {\", \".join(live) or \"none\"}{N}')
raise SystemExit(0 if total else 4)
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
  elif echo "$tools" | grep -qx "find_tool"; then
    # Optimizer on: search (QUERY) or enumerate the real tools behind find_tool.
    [[ -n "$QUERY" ]] && echo -e "  ${CYAN}search: \"${QUERY}\"${NC}"
    if discover_tools "$url" "$SESSION_ID" "$QUERY"; then
      ((PASS++))
    else
      echo -e "  ${YELLOW}? find_tool surfaced no backend tools — index empty or all backends down${NC}"
      ((WARN++))
    fi
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
