#!/usr/bin/env bash
# Print the dashboard basic-auth username + password for an agent.
#
# Each agent's random password lives in its own Kubernetes Secret
# (<agent>-secrets, key `password`) in the agent-sandbox namespace, mounted only
# into that agent's pod. No salt, no derivation, no shared secret — one agent
# cannot read or compute another's credentials.
#
#   username = <agent name>
#   password = Secret agent-sandbox/<agent>-secrets key `password`
set -euo pipefail

NAMESPACE="${HERMES_DASHBOARD_NAMESPACE:-agent-sandbox}"
DEFAULT_NODEPORT="${HERMES_DASHBOARD_NODEPORT:-30119}"
TARGET_CONTEXT="${KUBECONFIG_CONTEXT:-${KUBE_CONTEXT:-kind-vicegerent}}"
CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
[[ "$CURRENT_CONTEXT" == "$TARGET_CONTEXT" ]] || {
  echo "ERROR: current kubectl context is '${CURRENT_CONTEXT:-<none>}', expected '$TARGET_CONTEXT'. Run: kubectl config use-context $TARGET_CONTEXT" >&2
  exit 1
}
CONTEXT_ARG=(--context "$TARGET_CONTEXT")

usage() {
  echo "usage: $0 <agent-name>" >&2
  echo "  prints the dashboard basic-auth username and password for that agent" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
name="$1"
[ -n "$name" ] || usage
name="$(echo "$name" | tr '[:upper:]' '[:lower:]')"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 1; }

password="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get secret "${name}-secrets" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
[ -n "$password" ] || {
  echo "No password in Secret ${NAMESPACE}/${name}-secrets. Run: ./vicegerent secrets setup agent ${name}" >&2
  exit 1
}

SERVICE="${HERMES_DASHBOARD_SERVICE:-${name}-dashboard}"
node_port="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get svc "$SERVICE" -o jsonpath='{.spec.ports[?(@.name=="dashboard")].nodePort}' 2>/dev/null || true)"
[ -n "$node_port" ] || node_port="$DEFAULT_NODEPORT"

echo "username: ${name}"
echo "password: ${password}"
echo "url:      http://127.0.0.1:${node_port}/"
