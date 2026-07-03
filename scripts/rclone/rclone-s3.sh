#!/usr/bin/env bash
# Run the host-side `rclone serve s3` backend that stores Velero backups, reading its
# SigV4 auth-key from the local host file setup-secrets-platform.sh writes. The matching
# S3 credentials also live in the velero/velero-credentials Kubernetes Secret; this file
# is the host copy and is disposable — re-run setup-secrets-platform.sh to regenerate it.
# The served directory is a folder in this repo checkout and is gitignored.
set -euo pipefail

RCLONE_S3_HOST_DIR="${RCLONE_S3_HOST_DIR:-$HOME/.vicegerent/rclone-s3}"
# Bind loopback only. Kind reaches this via host.docker.internal (Docker Desktop
# proxies it to the host's localhost); binding 0.0.0.0 would expose it to the LAN.
ADDR="${ADDR:-127.0.0.1:9899}"
SERVE_DIR="${SERVE_DIR:?SERVE_DIR must be set (the rclone serve root)}"
BUCKET="${BUCKET:-vicegerent}"
RCLONE="${RCLONE:-rclone}"

AUTH_KEY_FILE="$RCLONE_S3_HOST_DIR/auth-key"
if [[ ! -s "$AUTH_KEY_FILE" ]]; then
  echo "missing $AUTH_KEY_FILE — start via './vicegerent start' (recovers it from the kind velero-credentials Secret), or run scripts/install/setup-secrets-platform.sh to (re)generate it." >&2
  exit 1
fi

# `rclone serve s3` treats top-level directories under the served root as buckets and
# ignores files there; pre-create the Velero bucket so its first PutObject succeeds.
mkdir -p "$SERVE_DIR/$BUCKET"

# exec so supervisord's SIGTERM reaches rclone directly.
exec "$RCLONE" serve s3 \
  --addr "$ADDR" \
  --auth-key "$(cat "$AUTH_KEY_FILE")" \
  --no-cleanup \
  "$SERVE_DIR"
