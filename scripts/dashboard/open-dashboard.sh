#!/usr/bin/env bash
# Open a Hermes agent dashboard directly in the browser.
#
# The dashboard Service is a NodePort (30119) published to the host loopback by
# the Kind cluster's extraPortMappings, so http://127.0.0.1:30119/ reaches it
# from macOS without a port-forward. Log in with the agent's basic-auth
# credentials (see dashboard-basic-cred.sh).
set -euo pipefail

NAMESPACE="${HERMES_DASHBOARD_NAMESPACE:-agent-sandbox}"
DASHBOARD_PATH="${HERMES_DASHBOARD_PATH:-/}"
[[ "$DASHBOARD_PATH" == /* ]] || DASHBOARD_PATH="/$DASHBOARD_PATH"
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
  echo "  opens that agent's Hermes dashboard in the browser" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
AGENT="$1"
[[ -n "$AGENT" ]] || usage
AGENT="$(echo "$AGENT" | tr '[:upper:]' '[:lower:]')"
SERVICE="${HERMES_DASHBOARD_SERVICE:-${AGENT}-dashboard}"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 1; }

node_port="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get svc "$SERVICE" -o jsonpath='{.spec.ports[?(@.name=="dashboard")].nodePort}' 2>/dev/null || true)"
[[ -n "$node_port" ]] || node_port="$DEFAULT_NODEPORT"

url="http://127.0.0.1:${node_port}${DASHBOARD_PATH}"

if command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
else
  echo "Open this URL: $url"
fi

echo "Hermes dashboard (${AGENT}): $url"
echo "Log in as '${AGENT}'; get the password with: ./vicegerent agent creds ${AGENT}"
