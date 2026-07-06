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
  local tmp_dir tmp_tar
  tmp_dir="$(mktemp -d /tmp/flux-schemas.XXXXXX)"
  tmp_tar="$tmp_dir/schemas.tar.gz"
  curl -sfL --connect-timeout 10 --max-time 60 "$SCHEMA_URL" -o "$tmp_tar"
  tar zxf "$tmp_tar" -C "$SCHEMA_DEST"
  rm -rf "$tmp_dir"
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
    -path './docs/available-mcp-tools/*' -prune -o \
    -type f -name '*.yaml' -print0
}

repo_kustomizations() {
  find . \
    -path './.git' -prune -o \
    -path './.cache' -prune -o \
    -path './.flux-crd-schemas' -prune -o \
    -type f -name "$kustomize_config" -print0
}

# Resolve Flux ${var} substitution tokens in a copy of the Cerbos policy dir from
# the committed cluster-vars ConfigMap, so `cerbos compile` exercises the real
# substituted values — it has no concept of Flux's postBuild syntax and would
# fail to parse raw ${...} tokens in the CEL expressions.
resolve_cluster_vars() {
  local vars_file="$1" target_dir="$2"
  command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR - python3 required to resolve cluster-vars tokens" >&2; return 1; }
  yq -o=json '.data' "$vars_file" | python3 -c '
import json, os, re, sys
data = json.load(sys.stdin)
target = sys.argv[1]
files = [os.path.join(target, f) for f in os.listdir(target) if f.endswith(".yaml")]
for f in files:
    s = open(f).read()
    for k, v in data.items():
        s = s.replace("${" + k + "}", v)
    open(f, "w").write(s)
leftover = set()
for f in files:
    leftover |= set(re.findall(r"\$\{[A-Za-z0-9_]+\}", open(f).read()))
if leftover:
    sys.stderr.write("ERROR - unresolved cluster-vars tokens: %s\n" % sorted(leftover))
    sys.exit(1)
' "$target_dir"
}

# Resolve Flux ${var} tokens into the egress-proxy overlay values, render the
# charts/egress-proxy chart with them, and assert the templated scrub.py is still
# valid Python. The allowlist literals are built by Helm {{ range }} over
# cluster-vars-sourced values; a bad value or template would produce YAML that
# survives kustomize build + kubeconform (just a string) then fail at mitmproxy
# load time in a live pod — so catch it loudly here at CI time.
validate_egress_scrub_py() {
  local vars_file="$1" values_file="$2" chart_dir="$3"
  command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR - python3 required to resolve cluster-vars tokens" >&2; return 1; }
  command -v helm >/dev/null 2>&1 \
    || { echo "WARN - helm not installed; skipping egress-proxy scrub.py validation" >&2; return 0; }
  local resolved_dir resolved
  resolved_dir="$(mktemp -d "${TMPDIR:-/tmp}/egress-values.XXXXXX")"
  resolved="$resolved_dir/values.yaml"
  yq -o=json '.data' "$vars_file" | python3 -c '
import json, re, sys
data = json.load(sys.stdin)
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
for k, v in data.items():
    s = s.replace("${" + k + "}", v)
leftover = sorted(set(re.findall(r"\$\{[A-Za-z0-9_]+\}", s)))
if leftover:
    sys.stderr.write("ERROR - unresolved cluster-vars tokens in %s: %s\n" % (src, leftover))
    sys.exit(1)
open(dst, "w").write(s)
' "$values_file" "$resolved" || { rm -rf "$resolved_dir"; return 1; }
  helm template egress-proxy "$chart_dir" -f "$resolved" --show-only templates/addon-configmap.yaml \
    | yq '.data."scrub.py"' \
    | python3 -c 'import ast, sys; ast.parse(sys.stdin.read())' \
    || { echo "ERROR - templated egress-proxy scrub.py is not valid Python" >&2; rm -rf "$resolved_dir"; return 1; }
  rm -rf "$resolved_dir"
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
cluster_vars_file="clusters/work/cluster-vars.yaml"
if [[ -d "$cerbos_policy_dir" ]]; then
  if command -v cerbos >/dev/null 2>&1; then
    echo "INFO - Compiling and testing Cerbos policies"
    if [[ -f "$cluster_vars_file" ]]; then
      resolved_dir="$(mktemp -d "${TMPDIR:-/tmp}/cerbos-defs.XXXXXX")"
      trap 'rm -rf "$resolved_dir"' EXIT
      cp "$cerbos_policy_dir"/*.yaml "$resolved_dir"/
      resolve_cluster_vars "$cluster_vars_file" "$resolved_dir"
      cerbos compile "$resolved_dir"
      rm -rf "$resolved_dir"
      trap - EXIT
    else
      cerbos compile "$cerbos_policy_dir"
    fi
  else
    echo "WARN - cerbos not installed; skipping Cerbos policy tests"
  fi

  # Cerbos test FIXTURES only pin values for the work cluster above (a fixture
  # would need per-cluster IDs to also assert against personal/etc, which
  # isn't worth the upkeep) -- but every cluster's cluster-vars.yaml still
  # needs to produce SYNTACTICALLY VALID CEL once substituted, regardless of
  # fixture coverage. A bug that's invisible in work's substitution (e.g. an
  # inline `# comment` baked into a multi-line YAML folded scalar `>-` value,
  # which is literal text there, not a real YAML comment) can still break a
  # DIFFERENT cluster's substitution outright at the Cerbos PDP -- confirmed
  # happening in practice (personal's jiraAllowedProjects derailed the whole
  # deny-write-outside-allowed-projects CEL expression into a parse error
  # only visible once actually deployed on that cluster). This loop does a
  # compile-only pass (no cerbos test assertions, since fixtures aren't
  # cluster-specific) over every OTHER cluster's cluster-vars.yaml to catch
  # that class of bug before merge instead of after a live deploy.
  if command -v cerbos >/dev/null 2>&1; then
    for other_vars_file in clusters/*/cluster-vars.yaml; do
      [[ "$other_vars_file" == "$cluster_vars_file" ]] && continue
      [[ -f "$other_vars_file" ]] || continue
      echo "INFO - Compile-only Cerbos check against $other_vars_file"
      other_resolved_dir="$(mktemp -d "${TMPDIR:-/tmp}/cerbos-defs-other.XXXXXX")"
      trap 'rm -rf "$other_resolved_dir"' EXIT
      cp "$cerbos_policy_dir"/*.yaml "$other_resolved_dir"/
      resolve_cluster_vars "$other_vars_file" "$other_resolved_dir"
      cerbos compile --skip-tests "$other_resolved_dir" || {
        echo "ERROR - Cerbos policy fails to compile against $other_vars_file" >&2
        exit 1
      }
      rm -rf "$other_resolved_dir"
      trap - EXIT
    done
  fi
fi

egress_values_file="apps/work/egress-proxy/values.yaml"
egress_chart_dir="charts/egress-proxy"
if [[ -f "$egress_values_file" && -f "$cluster_vars_file" ]]; then
  echo "INFO - Rendering egress-proxy chart with cluster-vars and validating scrub.py"
  validate_egress_scrub_py "$cluster_vars_file" "$egress_values_file" "$egress_chart_dir"
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
