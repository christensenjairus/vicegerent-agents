#!/usr/bin/env bash
# Run the host-side ghostunnel server in the foreground, reading its mTLS
# material from the local host directory that setup-secrets-platform.sh writes.
# The cluster gets the matching client cert + CA cert as Kubernetes Secrets; the
# server key never enters Kubernetes. These files are the source of truth for the
# laptop side and are disposable — re-run setup-secrets-platform.sh to regenerate.
set -euo pipefail

GHOSTUNNEL_HOST_DIR="${GHOSTUNNEL_HOST_DIR:-$HOME/.vicegerent/ghostunnel}"
# Bind loopback only. Kind reaches this via host.docker.internal (Docker Desktop
# proxies it to the host's localhost); binding 0.0.0.0 would expose it to the LAN.
LISTEN="${LISTEN:-127.0.0.1:8453}"
TARGET="${TARGET:-127.0.0.1:4483}"
ALLOW_CN="${ALLOW_CN:-agent-client}"
GHOSTUNNEL="${GHOSTUNNEL:-ghostunnel}"

for f in server.crt server.key ca.cert; do
  if [[ ! -s "$GHOSTUNNEL_HOST_DIR/$f" ]]; then
    echo "missing $GHOSTUNNEL_HOST_DIR/$f — start via './vicegerent start' (recovers it from the kind ghostunnel-server Secret), or run scripts/install/setup-secrets-platform.sh to (re)generate it." >&2
    exit 1
  fi
done

# Run ghostunnel as a child and forward TERM/INT to it so supervisord's
# SIGTERM reaches ghostunnel directly.
"$GHOSTUNNEL" server \
  --listen "$LISTEN" \
  --target "$TARGET" \
  --cert "$GHOSTUNNEL_HOST_DIR/server.crt" \
  --key "$GHOSTUNNEL_HOST_DIR/server.key" \
  --cacert "$GHOSTUNNEL_HOST_DIR/ca.cert" \
  --allow-cn "$ALLOW_CN" &
GHOSTUNNEL_PID=$!
forward_signal() { kill -s "$1" "$GHOSTUNNEL_PID" 2>/dev/null || true; }
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
wait "$GHOSTUNNEL_PID"
