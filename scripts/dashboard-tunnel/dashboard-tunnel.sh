#!/usr/bin/env bash
# Host-side dashboard tunnel: run a ghostunnel client per agent so Hermes
# Desktop can reach each sandbox's loopback-bound dashboard over mTLS.
#
# Why a host-side loopback listener is mandatory (not just port-forward
# avoidance): the in-pod dashboard binds 127.0.0.1 and rejects any request
# whose Host header isn't a loopback name (DNS-rebinding defense). So the only
# way Desktop can drive it is to connect to a LOCAL 127.0.0.1 listener — which
# this client provides. Each agent gets its own local port:
#
#   127.0.0.1:9119 -> hermes   (NodePort 30119 on the minikube node)
#   127.0.0.1:9120 -> <agent2> (NodePort 30120) ...
#
# mTLS (client cert from 1Password "Dashboard Tunnel") is the network auth; the
# dashboard's own basic-auth login (username/password) is the app-layer auth —
# get an agent's credentials with dashboard-basic-cred.sh. Point Hermes
# Desktop's "Remote gateway" at http://127.0.0.1:<port> for the agent you want.
#
# Run in the foreground for an interactive session, or install the launchd
# agent (see scripts/dashboard-tunnel/README.md) to keep it up across reboots.
#
# Agents to tunnel can be passed as positional args, one "name:local:node" spec
# each; with no args it falls back to the built-in default below:
#
#   dashboard-tunnel.sh hermes:9119:30119 agent2:9120:30120
set -euo pipefail

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "Usage: ${0##*/} [name:local_port:node_port ...]"
  exit "${1:-0}"
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage 0

OP_VAULT="${OP_VAULT:-Vicegerent}"
OP_ITEM="${OP_ITEM:-Dashboard Tunnel}"
GHOSTUNNEL="${GHOSTUNNEL:-ghostunnel}"

# Minikube node IP the NodePort is exposed on (host-only network). Auto-detect
# from the vicegerent profile, override with NODE_IP=...
NODE_IP="${NODE_IP:-$(minikube -p vicegerent ip 2>/dev/null || true)}"
if [[ -z "${NODE_IP}" ]]; then
  echo "Could not determine the minikube node IP. Set NODE_IP=... (e.g. NODE_IP=\$(minikube -p vicegerent ip))." >&2
  exit 1
fi

# Agent -> "local_port:node_port" map. Defaults below; override by passing one
# or more "name:local_port:node_port" specs as positional args.
# Keep local 9119/9120/... aligned with node 30119/30120/... by convention.
AGENTS=(
  "hermes:9119:30119"
)
if [[ $# -gt 0 ]]; then
  AGENTS=("$@")
fi

# Validate each spec is exactly name:local_port:node_port with numeric ports.
for entry in "${AGENTS[@]}"; do
  if [[ ! "$entry" =~ ^[A-Za-z0-9_-]+:[0-9]+:[0-9]+$ ]]; then
    echo "Invalid agent spec '${entry}'. Expected name:local_port:node_port (e.g. hermes:9119:30119)." >&2
    exit 2
  fi
done

command -v "$GHOSTUNNEL" >/dev/null 2>&1 || {
  echo "ghostunnel not found on PATH. brew install ghostunnel (or set GHOSTUNNEL=...)." >&2
  exit 1
}
op account get >/dev/null 2>&1 || {
  echo "1Password CLI is not signed in. Run: op signin" >&2
  exit 1
}

# Pull the client material into a private tmpdir wiped on exit. For the launchd
# daemon this process is long-lived, so the material lives only as long as the
# tunnel and is removed the moment it stops.
CERTS="$(mktemp -d "${TMPDIR:-/tmp}/dash-tunnel.XXXXXX")"
chmod 700 "$CERTS"
PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  rm -rf "$CERTS"
}
trap cleanup EXIT INT TERM

op read "op://${OP_VAULT}/${OP_ITEM}/client.crt" > "$CERTS/client.crt"
op read "op://${OP_VAULT}/${OP_ITEM}/client.key" > "$CERTS/client.key"
op read "op://${OP_VAULT}/${OP_ITEM}/ca.cert"    > "$CERTS/ca.cert"
chmod 600 "$CERTS"/*

for entry in "${AGENTS[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  local_port="${rest%%:*}"
  node_port="${rest#*:}"
  echo "tunnel: 127.0.0.1:${local_port} -> ${name} (mTLS ${NODE_IP}:${node_port})"
  # ghostunnel client: listen on loopback, dial the NodePort with mutual TLS.
  # The server cert SAN includes the node IP, so connecting by IP verifies.
  "$GHOSTUNNEL" client \
    --listen "127.0.0.1:${local_port}" \
    --target "${NODE_IP}:${node_port}" \
    --cert "$CERTS/client.crt" \
    --key "$CERTS/client.key" \
    --cacert "$CERTS/ca.cert" &
  PIDS+=("$!")
done

echo "Dashboard tunnels up. Point Hermes Desktop at http://127.0.0.1:<port>. Ctrl-C to stop."
wait
