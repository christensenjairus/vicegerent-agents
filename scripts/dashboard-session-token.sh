#!/usr/bin/env bash
# Deterministically derive a Hermes dashboard session token from a sandbox
# name. The same value is computed in two places without ever exchanging a
# secret:
#   - the cluster, via the Kustomize secretGenerator literal in the agent's
#     kustomization.yaml, wired into HERMES_DASHBOARD_SESSION_TOKEN so the
#     dashboard's loopback session token is STABLE across pod restarts
#     (otherwise Hermes regenerates a random token each boot and Hermes
#     Desktop's saved connection breaks on every restart);
#   - the host, so the operator can compute the token for a given agent
#     without reading it out of the cluster.
#
# This token is NOT the network security boundary. The dashboard binds
# loopback inside the pod; the only network path in is the ghostunnel mTLS
# tunnel, whose client certificate is the actual transport auth. The session
# token is the dashboard's own app-layer handshake value
# (X-Hermes-Session-Token). Because it is a pure function of the (public)
# sandbox name, treat it as reproducible, not confidential.
set -euo pipefail

usage() {
  echo "usage: $0 <sandbox-name>" >&2
  echo "  prints the deterministic dashboard session token for that sandbox" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
name="$1"
[ -n "$name" ] || usage

# Namespaced derivation string so the token can't collide with any other
# sha256-of-a-bare-name value elsewhere in the platform.
msg="vicegerent/agent-sandbox/${name}/dashboard-session/v1"

# URL-safe base64 of the raw SHA-256 digest (43 chars, no padding). openssl
# is present on both macOS and the Linux image, so this runs identically on
# the host and in CI.
printf '%s' "$msg" \
  | openssl dgst -sha256 -binary \
  | openssl base64 -A \
  | tr '+/' '-_' \
  | tr -d '='
