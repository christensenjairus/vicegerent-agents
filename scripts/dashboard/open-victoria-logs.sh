#!/usr/bin/env bash
# Port-forward VictoriaLogs' server Service and open its web UI (vmui) in the
# browser. Blocks until Ctrl+C, then tears down the port-forward — same
# foreground-until-interrupted shape as `kubectl port-forward` itself.
set -euo pipefail

NAMESPACE="${VICTORIA_LOGS_NAMESPACE:-victoria-logs}"
SERVICE="${VICTORIA_LOGS_SERVICE:-victoria-logs-server}"
PORT="${VICTORIA_LOGS_PORT:-9428}"
TARGET_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"

CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
[[ "$CURRENT_CONTEXT" == "$TARGET_CONTEXT" ]] || {
  echo "ERROR: current kubectl context is '${CURRENT_CONTEXT:-<none>}', expected '$TARGET_CONTEXT'. Run: kubectl config use-context $TARGET_CONTEXT" >&2
  exit 1
}

kubectl --context "$TARGET_CONTEXT" -n "$NAMESPACE" port-forward "svc/$SERVICE" "$PORT:$PORT" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID:-}" 2>/dev/null || true' EXIT INT TERM

i=0
while ! python3 -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),0.25); s.close()" 2>/dev/null; do
  i=$((i + 1))
  if [[ $i -ge 60 ]]; then
    echo "ERROR: victoria-logs port-forward did not become ready after 15s" >&2
    exit 1
  fi
  sleep 0.25
done

url="http://127.0.0.1:${PORT}/select/vmui/"

if command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
else
  echo "Open this URL: $url"
fi

echo "VictoriaLogs dashboard: $url"
echo "Port-forward running (pid $PF_PID). Press Ctrl+C to stop."
wait "$PF_PID"
