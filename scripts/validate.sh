#!/usr/bin/env bash

# This script downloads Flux OpenAPI schemas, then validates Flux custom
# resources and kustomize overlays using kubeconform.

set -o errexit
set -o pipefail

RETRIES=5
WAIT=2
FLUX_VERSION="${FLUX_VERSION:-v2.7.0}"
SCHEMA_URL="https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/crd-schemas.tar.gz"
SCHEMA_DEST="${SCHEMA_DEST:-/tmp/flux-crd-schemas/master-standalone-strict}"
SCHEMA_VERSION_FILE="$SCHEMA_DEST/.schema-version"

kustomize_flags=("--load-restrictor=LoadRestrictionsNone")
kustomize_config="kustomization.yaml"
kubeconform_flags=("-skip=Secret")
kubeconform_config=("-strict" "-ignore-missing-schemas" "-schema-location" "default" "-schema-location" "$(dirname "$SCHEMA_DEST")" "-verbose")

retry_cmd() {
  local n=1
  until "$@"; do
    if (( n >= RETRIES )); then
      echo "ERROR - Command failed after $n attempts: $*"
      return 1
    fi
    echo "WARN - Attempt $n/$RETRIES failed. Retrying in ${WAIT}s..."
    sleep "$WAIT"
    ((n++))
  done
}

require_tools() {
  local missing=0
  for cmd in curl tar yq kustomize kubeconform; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "ERROR - $cmd is not installed" >&2
      missing=1
    fi
  done
  if (( missing != 0 )); then
    exit 1
  fi
}

download_and_extract_schemas() {
  local tmp_tar
  tmp_tar="$(mktemp /tmp/flux-schemas.XXXXXX.tar.gz)"
  curl -sfL --connect-timeout 10 --max-time 60 "$SCHEMA_URL" -o "$tmp_tar"
  tar zxf "$tmp_tar" -C "$SCHEMA_DEST"
  rm -f "$tmp_tar"
}

ensure_flux_schemas() {
  mkdir -p "$SCHEMA_DEST"

  if [[ -f "$SCHEMA_VERSION_FILE" ]] && \
     [[ "$(cat "$SCHEMA_VERSION_FILE")" == "$FLUX_VERSION" ]] && \
     [[ -n "$(find "$SCHEMA_DEST" -type f ! -name .schema-version -print -quit 2>/dev/null)" ]]; then
    echo "INFO - Using cached Flux OpenAPI schemas from $SCHEMA_DEST ($FLUX_VERSION)"
    return
  fi

  echo "INFO - Downloading Flux OpenAPI schemas ($FLUX_VERSION)"
  rm -rf "$SCHEMA_DEST"
  mkdir -p "$SCHEMA_DEST"
  retry_cmd download_and_extract_schemas
  echo "$FLUX_VERSION" > "$SCHEMA_VERSION_FILE"
}

repo_yaml_files() {
  find . \
    -path './.git' -prune -o \
    -path './.cache' -prune -o \
    -path './.flux-crd-schemas' -prune -o \
    -path './charts/*/templates/*' -prune -o \
    -type f -name '*.yaml' -print0
}

repo_kustomizations() {
  find . \
    -path './.git' -prune -o \
    -path './.cache' -prune -o \
    -path './.flux-crd-schemas' -prune -o \
    -type f -name "$kustomize_config" -print0
}

require_tools
ensure_flux_schemas

echo "INFO - Validating YAML syntax"
repo_yaml_files | while IFS= read -r -d $'\0' file; do
  echo "INFO - Validating $file"
  yq e 'true' "$file" >/dev/null
done

if [[ -d ./charts ]]; then
  echo "INFO - Linting Helm charts"
  find ./charts -maxdepth 1 -mindepth 1 -type d -print0 | while IFS= read -r -d $'\0' chart; do
    echo "INFO - Linting $chart"
    helm lint "$chart"
  done
fi

echo "INFO - Validating cluster manifests"
find ./clusters -type f -name '*.yaml' -print0 | while IFS= read -r -d $'\0' file; do
  echo "INFO - Validating $file"
  retry_cmd kubeconform "${kubeconform_flags[@]}" "${kubeconform_config[@]}" "$file"
done

echo "INFO - Validating kustomize overlays"
repo_kustomizations | while IFS= read -r -d $'\0' file; do
  dir="${file/%$kustomize_config}"
  echo "INFO - Validating kustomization $dir"
  retry_cmd bash -c "kustomize build '$dir' ${kustomize_flags[*]} | kubeconform ${kubeconform_flags[*]} ${kubeconform_config[*]}"
done

cerbos_policy_dir="infrastructure/controllers/cerbos/policies/defs"
if [[ -d "$cerbos_policy_dir" ]]; then
  if command -v cerbos >/dev/null 2>&1; then
    echo "INFO - Compiling and testing Cerbos policies"
    cerbos compile "$cerbos_policy_dir"
  else
    echo "WARN - cerbos not installed; skipping Cerbos policy tests"
  fi
fi

# Assert the vMCP overlay's AgentgatewayPolicy either attaches a well-formed
# Cerbos guardrail or attaches none at all — never a malformed/partial one. A
# guardrail is well-formed only as a single tools/call -> mcp-cerbos-shim
# processor with FailClosed; anything else forwards to the MCP server with no
# policy check (a silent fail-open). This catches a dropped, renamed, or
# weakened guardrail at MR time. It does NOT cover the runtime reconcile path
# (Flux never applying the commit, or the controller silently rejecting the
# CRD) — that gap is documented in the shim README.
#
# The vMCP backend (hand-written Flux YAML) is required to carry the guardrail
# — its Secret-blocking model depends on it.
assert_guardrail_well_formed() {
  local overlay="$1" rendered="$2"
  local processors
  processors="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayPolicy")
    | .spec.backend.mcp.guardrails.processors // [] | length' -)"
  if [[ "$processors" == "0" || -z "$processors" ]]; then
    return
  fi
  local well_formed
  well_formed="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayPolicy")
    | [ .spec.backend.mcp.guardrails.processors[]
        | select(.methods["tools/call"] == "Request"
            and .remote.backendRef.name == "mcp-cerbos-shim"
            and .remote.failureMode == "FailClosed") ]
    | length' -)"
  if [[ "$processors" != "1" || "$well_formed" != "1" ]]; then
    echo "ERROR - $overlay AgentgatewayPolicy has a malformed Cerbos guardrail" \
         "(found $processors processor(s), $well_formed well-formed). It must be" \
         "exactly one tools/call -> mcp-cerbos-shim processor with FailClosed." >&2
    exit 1
  fi
}

echo "INFO - Asserting MCP Cerbos guardrails are well-formed"
vmcp_overlay="apps/base/mcps/vmcp"
if [[ ! -d "$vmcp_overlay" ]]; then
  echo "ERROR - $vmcp_overlay not found; cannot verify the vMCP Cerbos guardrail." >&2
  exit 1
fi

# vMCP is hand-written Flux YAML — kustomize build renders it directly.
vmcp_rendered="$(kustomize build "$vmcp_overlay" "${kustomize_flags[@]}")"
assert_guardrail_well_formed "$vmcp_overlay" "$vmcp_rendered"
vmcp_guardrail_processors="$(echo "$vmcp_rendered" | yq ea '
  select(.kind == "AgentgatewayPolicy")
  | .spec.backend.mcp.guardrails.processors // [] | length' -)"
if [[ "$vmcp_guardrail_processors" != "1" ]]; then
  echo "ERROR - vMCP AgentgatewayPolicy must attach exactly one" \
       "tools/call -> mcp-cerbos-shim guardrail with FailClosed (found:" \
       "${vmcp_guardrail_processors:-0}). Refusing to ship a fail-open MCP backend." >&2
  exit 1
fi

echo "INFO - All validations passed"
