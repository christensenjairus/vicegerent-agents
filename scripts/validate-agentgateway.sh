#!/usr/bin/env bash
# Validate rendered agentgateway custom resources against the CRD schema pinned
# in the repo. The CRD chart version is read from the deployed HelmRelease so it
# can never drift from what's actually running. See
# scripts/validate-agentgateway-crds.py for why this gate exists.
set -o errexit
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

RELEASE="infrastructure/controllers/agentgateway/crds/chart/release.yaml"
CRD_VERSION="$(yq -r '.spec.chart.spec.version' "$RELEASE")"
CRD_REPO="$(yq -r '.spec.chart.spec.sourceRef.name' "$RELEASE")"
echo "INFO - agentgateway-crds version pinned to ${CRD_VERSION} (from ${RELEASE})"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Pull + unpack the same CRD chart the cluster deploys.
helm pull "oci://cr.agentgateway.dev/charts/agentgateway-crds" \
  --version "${CRD_VERSION}" -d "$WORK" >/dev/null
tar xzf "$WORK"/agentgateway-crds-*.tgz -C "$WORK"
CRD_GLOB="$WORK/agentgateway-crds/templates/*.yaml"

# Render every overlay that contains an agentgateway custom resource and
# validate it. Discover them by grepping for the API group so new MCP/model
# backends are picked up automatically. Portable to BusyBox (no GNU grep
# --include, no xargs -r): collect matches into an array, dedupe in awk.
mapfile -t DIRS < <(
  find apps infrastructure -type f -name '*.yaml' -print0 \
    | xargs -0 grep -l 'agentgateway.dev/v1' \
    | while IFS= read -r f; do dirname "$f"; done \
    | awk '!seen[$0]++'
)
echo "INFO - discovered ${#DIRS[@]} overlay dir(s) with agentgateway resources"

RENDERED="$WORK/rendered.yaml"
: > "$RENDERED"
for d in "${DIRS[@]}"; do
  [ -f "$d/kustomization.yaml" ] || continue
  echo "INFO - rendering $d"
  kustomize build --load-restrictor=LoadRestrictionsNone "$d" >> "$RENDERED"
  echo "---" >> "$RENDERED"
done

python3 scripts/validate-agentgateway-crds.py "$CRD_GLOB" "$RENDERED"
