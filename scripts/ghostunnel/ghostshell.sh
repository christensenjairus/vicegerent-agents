#!/usr/bin/env bash
# Pull the host-side ghostunnel material from 1Password into an ephemeral
# tmpdir, run the ghostunnel server in the foreground, and wipe the material
# on exit. No mTLS key or cert is ever persisted on the laptop: 1Password is
# the single source of truth for both the laptop and the cluster.
set -euo pipefail

OP_VAULT="${OP_VAULT:-Vicegerent}"
OP_HOST_ITEM="${OP_HOST_ITEM:-Ghostunnel Host}"
HOST_ONLY_IP="${HOST_ONLY_IP:-192.168.64.1}"
LISTEN="${LISTEN:-${HOST_ONLY_IP}:8453}"
TARGET="${TARGET:-127.0.0.1:8080}"
ALLOW_CN="${ALLOW_CN:-agent-client}"
GHOSTUNNEL="${GHOSTUNNEL:-ghostunnel}"

op account get >/dev/null 2>&1 || {
  echo "1Password CLI is not signed in. Run: op signin" >&2
  exit 1
}

CERTS="$(mktemp -d "${TMPDIR:-/tmp}/ghostshell.XXXXXX")"
chmod 700 "$CERTS"
cleanup() { rm -rf "$CERTS"; }
trap cleanup EXIT

for f in server.crt server.key ca.cert; do
  op read "op://${OP_VAULT}/${OP_HOST_ITEM}/${f}" >"$CERTS/$f"
done
chmod 600 "$CERTS"/*

# Run ghostunnel as a child and forward TERM/INT to it so supervisord's
# SIGTERM reaches ghostunnel directly. The EXIT trap then wipes the certs.
"$GHOSTUNNEL" server \
  --listen "$LISTEN" \
  --target "$TARGET" \
  --cert "$CERTS/server.crt" \
  --key "$CERTS/server.key" \
  --cacert "$CERTS/ca.cert" \
  --allow-cn "$ALLOW_CN" &
GHOSTUNNEL_PID=$!
forward_signal() { kill -s "$1" "$GHOSTUNNEL_PID" 2>/dev/null || true; }
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
wait "$GHOSTUNNEL_PID"
