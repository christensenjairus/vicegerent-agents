#!/usr/bin/env bash
# Print the dashboard basic-auth username + password for an agent.
#
# Each agent has its OWN 1Password item ("Dashboard Auth - <agent>") holding a
# random password, mounted only into that agent's pod. No salt, no derivation,
# no shared secret — one agent cannot read or compute another's credentials.
#
#   username = <agent name>
#   password = op://<vault>/Dashboard Auth - <agent>/password
set -euo pipefail

OP_VAULT="${OP_VAULT:-Vicegerent}"

usage() {
  echo "usage: $0 <agent-name>" >&2
  echo "  prints the dashboard basic-auth username and password for that agent" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
name="$1"
[ -n "$name" ] || usage

command -v op >/dev/null 2>&1 || {
  echo "1Password CLI not found. brew install 1password-cli." >&2
  exit 1
}
op account get >/dev/null 2>&1 || {
  echo "1Password CLI is not signed in. Run: op signin" >&2
  exit 1
}

item="Dashboard Auth - ${name}"
password="$(op read "op://${OP_VAULT}/${item}/password")"
[ -n "$password" ] || {
  echo "No password in 1Password item '${item}'. Run scripts/install/setup-secrets.sh (with DASHBOARD_AUTH_AGENTS including '${name}')." >&2
  exit 1
}

echo "username: ${name}"
echo "password: ${password}"
