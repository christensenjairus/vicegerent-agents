#!/usr/bin/env bash
# Idempotent bootstrap of the vicegerent platform onto a local Kind cluster.
#
# Runs `flux bootstrap git`. Secrets are provisioned separately and directly as
# Kubernetes Secrets by setup-secrets-platform.sh / setup-secrets-agent.sh — run
# those before (or right after) bootstrap so the workloads Flux reconciles have
# the material they consume. `flux bootstrap git` is idempotent and re-applies
# cleanly, so this script is safe to re-run.
#
# Flags:
#   -y, --yes           auto-approve every change (non-interactive)
#   -h, --help          show this help
#
# Env overrides: KUBE_CONTEXT, REPO_URL, BRANCH, CLUSTER_PATH, PRIVATE_KEY_FILE
# REPO_URL defaults to this checkout's 'origin' remote (normalized to an ssh://
# URL, which Flux's SSH bootstrap requires), so a fork bootstraps against itself
# without any override.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Flux SSH auth requires an ssh:// URL; convert SCP-style (git@host:path) and
# https remotes to it, and leave an ssh:// URL untouched.
normalize_ssh_url() {
  local url="$1"
  case "$url" in
    ""|ssh://*) printf '%s' "$url" ;;
    https://*)  printf 'ssh://git@%s' "${url#https://}" ;;
    *@*:*)      printf 'ssh://%s' "$(printf '%s' "$url" | sed 's|:|/|')" ;;
    *)          printf '%s' "$url" ;;
  esac
}

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"
REPO_URL="${REPO_URL:-$(normalize_ssh_url "$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)")}"
BRANCH="${BRANCH:-main}"
CLUSTER_PATH="${CLUSTER_PATH:-./clusters/personal}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-$HOME/.ssh/id_rsa}"

ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[0;33m'; R=$'\033[0;31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
info() { echo "${G}•${N} $*"; }
step() { echo; echo "${B}== $* ==${N}"; }
warn() { echo "${Y}!${N} $*" >&2; }
die()  { echo "${R}ERROR:${N} $*" >&2; exit 1; }

confirm() {
  echo
  echo "${Y}CHANGE:${N} $*"
  if [[ "$ASSUME_YES" == "1" ]]; then
    echo "  (auto-approved via --yes)"
    return 0
  fi
  local ans
  read -r -p "  Proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

# --- prerequisites ---------------------------------------------------------
step "Prerequisites"
for cmd in kubectl flux; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not installed or not on PATH"
done
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kubectl context '$KUBE_CONTEXT' does not exist"
current_ctx="$(kubectl config current-context 2>/dev/null || true)"
[[ "$current_ctx" == "$KUBE_CONTEXT" ]] \
  || die "current kubectl context is '${current_ctx:-<none>}', expected '$KUBE_CONTEXT' (run: kubectl config use-context $KUBE_CONTEXT)"
[[ -n "$REPO_URL" ]] \
  || die "REPO_URL not set and '$REPO_ROOT' has no 'origin' git remote; set REPO_URL to your fork's SSH URL"
[[ -f "$PRIVATE_KEY_FILE" ]] || die "private key not found: $PRIVATE_KEY_FILE"
info "All required tools present; context '$KUBE_CONTEXT' exists."

echo
echo "Target context: $KUBE_CONTEXT"
echo "Repository:     $REPO_URL"
echo "Branch:         $BRANCH"
echo "Cluster path:   $CLUSTER_PATH"
echo "Private key:    $PRIVATE_KEY_FILE"
kc cluster-info >/dev/null
kc get nodes -o wide

# --- Flux bootstrap --------------------------------------------------------
step "Flux bootstrap"
if kc get namespace flux-system >/dev/null 2>&1; then
  info "flux-system namespace exists; re-running bootstrap to reconcile (idempotent)."
else
  confirm "Bootstrap Flux onto context '$KUBE_CONTEXT' against $REPO_URL ($BRANCH, $CLUSTER_PATH)." \
    || die "Flux bootstrap declined; aborting."
fi

flux bootstrap git \
  --url="$REPO_URL" \
  --branch="$BRANCH" \
  --path="$CLUSTER_PATH" \
  --private-key-file="$PRIVATE_KEY_FILE" \
  --context="$KUBE_CONTEXT"

echo
info "${G}Bootstrap complete.${N}"
warn "If you have not already, provision secrets so workloads can start:"
echo "  ./vicegerent secrets setup platform"
echo "  ./vicegerent secrets setup agent <name>"
echo "Check reconciliation with:"
echo "  flux --context $KUBE_CONTEXT get all -A"
