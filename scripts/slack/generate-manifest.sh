#!/usr/bin/env bash
# generate-manifest.sh — generate a Hermes Slack app manifest for a named bot
#
# Usage:
#   ./generate-manifest.sh <name> [output-file]
#
# Arguments:
#   name         Bot display name, e.g. "Hermes Infra" or "Infra"
#   output-file  Where to write the manifest (default: stdout)
#
# Examples:
#   ./scripts/slack/generate-manifest.sh "Hermes Infra"
#   ./scripts/slack/generate-manifest.sh "Hermes Infra" infra-manifest.yaml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/hermes-manifest.template.yaml"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <name> [output-file]" >&2
  exit 1
fi

NAME="$1"
OUTPUT="${2:-}"

# Warn if the name is already all-lowercase — Slack display names should be title-cased.
if [[ "${NAME}" == "$(echo "${NAME}" | tr '[:upper:]' '[:lower:]')" ]]; then
  echo "warning: name '${NAME}' is all lowercase — Slack display names are typically title-cased (e.g. 'Hermes Infra')" >&2
fi

if [[ ! -f "${TEMPLATE}" ]]; then
  echo "Template not found: ${TEMPLATE}" >&2
  exit 1
fi

result="$(sed \
  -e "s|{{NAME}}|${NAME}|g" \
  "${TEMPLATE}")"

if [[ -n "${OUTPUT}" ]]; then
  echo "${result}" >"${OUTPUT}"
  echo "Wrote manifest to ${OUTPUT}" >&2
else
  echo "${result}"
fi
