#!/usr/bin/env bash
# Idempotent bootstrap of the vicegerent platform onto a local minikube cluster.
#
# Seeds 1Password Connect (so the operator can sync secrets Flux depends on),
# then runs `flux bootstrap git`. Safe to re-run:
#   - secrets are applied via create --dry-run | apply (no-op when unchanged)
#   - the Connect Helm release uses `helm upgrade --install`, and a release left
#     in a stuck/failed state by an interrupted run is recovered (rollback or
#     uninstall) before retrying, so an operator/chart change does not choke it
#   - once the release is healthy and adopted by Flux, the manual Helm seed is
#     skipped so it does not fight Flux's ownership
#   - `flux bootstrap git` is itself idempotent and re-applies cleanly
#
# Flags:
#   -y, --yes           auto-approve every change (non-interactive)
#   --reseed-secrets    re-apply the op-credentials + onepassword-token Secrets from
#                       1Password and restart Connect, even when the release is already
#                       deployed. Use after rotating the Connect token or credentials.
#                       Requires the Connect server to still exist (run setup-secrets.sh
#                       first if it was deleted).
#   -h, --help          show this help
#
# Env overrides: KUBE_CONTEXT, REPO_URL, BRANCH, CLUSTER_PATH, PRIVATE_KEY_FILE,
#   OP_CONNECT_CREDENTIALS_REF, OP_CONNECT_TOKEN_ITEM, OP_CONNECT_TOKEN_VAULT,
#   OP_CONNECT_TOKEN, SKIP_ONEPASSWORD_BOOTSTRAP=1, RESEED_CONNECT_SECRETS=1

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-vicegerent}"
REPO_URL="${REPO_URL:-ssh://git@gitlab.hahomelabs.com/jchristensen/vicegerent-agents.git}"
BRANCH="${BRANCH:-main}"
CLUSTER_PATH="${CLUSTER_PATH:-./clusters/vicegerent}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-$HOME/.ssh/id_rsa}"
OP_CONNECT_CREDENTIALS_REF="${OP_CONNECT_CREDENTIALS_REF:-op://Vicegerent/Connect Credentials/1password-credentials.json}"
OP_CONNECT_TOKEN_ITEM="${OP_CONNECT_TOKEN_ITEM:-Connect Token}"
OP_CONNECT_TOKEN_VAULT="${OP_CONNECT_TOKEN_VAULT:-Vicegerent}"
OP_CONNECT_SERVER="${OP_CONNECT_SERVER:-Vicegerent}"
SKIP_ONEPASSWORD_BOOTSTRAP="${SKIP_ONEPASSWORD_BOOTSTRAP:-0}"
RESEED_CONNECT_SECRETS="${RESEED_CONNECT_SECRETS:-0}"

CONNECT_NS="onepassword-connect"
CONNECT_RELEASE="connect"

ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --reseed-secrets) RESEED_CONNECT_SECRETS=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
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

helm_status() {
  helm status "$CONNECT_RELEASE" --namespace "$CONNECT_NS" --kube-context "$KUBE_CONTEXT" \
    -o json 2>/dev/null | jq -r '.info.status' 2>/dev/null || echo "absent"
}

connect_server_exists() {
  op connect server list --format json 2>/dev/null \
    | jq -e --arg n "$OP_CONNECT_SERVER" '.[]? | select(.name==$n)' >/dev/null 2>&1
}

# Read the Connect token + credentials from 1Password and apply them as the
# op-credentials + onepassword-token Secrets. Used both for the initial seed and
# for re-seeding after a rotation. `create --dry-run | apply` is a no-op when the
# material is unchanged and updates the Secret in place when it has rotated.
# These Secrets are intentionally NOT in the Flux tree (they are write-once
# bootstrap material), so applying them does not fight Flux ownership.
apply_connect_secrets() {
  local credentials_file token
  credentials_file="$(mktemp)"
  trap 'rm -f "$credentials_file"' RETURN

  token="${OP_CONNECT_TOKEN:-$(op item get "$OP_CONNECT_TOKEN_ITEM" --vault "$OP_CONNECT_TOKEN_VAULT" --format json | jq -r '([.fields[] | select(.label == "credential" or .label == "token") | .value] + [.fields[2].value]) | map(select(. != null and . != "")) | .[0]')}"
  [[ -n "$token" && "$token" != "null" ]] \
    || die "could not read 1Password Connect token from '$OP_CONNECT_TOKEN_ITEM'"
  op read "$OP_CONNECT_CREDENTIALS_REF" > "$credentials_file"
  [[ -s "$credentials_file" ]] || die "Connect credentials file is empty"

  kc create namespace "$CONNECT_NS" --dry-run=client -o yaml | kc apply -f -
  kc -n "$CONNECT_NS" create secret generic op-credentials \
    --from-file=1password-credentials.json="$credentials_file" \
    --dry-run=client -o yaml | kc apply -f -
  kc -n "$CONNECT_NS" create secret generic onepassword-token \
    --from-literal=token="$token" \
    --dry-run=client -o yaml | kc apply -f -
}

# Restart the Connect deployments so they re-read rotated credentials. The pods
# load the token + credentials at startup (operator.autoRestart is off), so a
# Secret update alone is not picked up until they roll. The namespace is
# dedicated to Connect, so restarting every deployment in it is safe.
restart_connect() {
  kc -n "$CONNECT_NS" rollout restart deployment --all >/dev/null
  kc -n "$CONNECT_NS" rollout status deployment --all --timeout=120s >/dev/null 2>&1 || true
}

# --- prerequisites ---------------------------------------------------------
step "Prerequisites"
required_cmds=(kubectl flux)
if [[ "$SKIP_ONEPASSWORD_BOOTSTRAP" != "1" ]]; then
  required_cmds+=(helm op jq)
fi
for cmd in "${required_cmds[@]}"; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not installed or not on PATH"
done
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kubectl context '$KUBE_CONTEXT' does not exist"
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

# --- 1Password Connect seed ------------------------------------------------
if [[ "$SKIP_ONEPASSWORD_BOOTSTRAP" != "1" ]]; then
  step "1Password Connect"
  op account get >/dev/null 2>&1 || die "1Password CLI is not signed in. Run: op signin"

  status="$(helm_status)"
  if [[ "$status" == "deployed" ]]; then
    if [[ "$RESEED_CONNECT_SECRETS" == "1" ]]; then
      connect_server_exists \
        || die "Connect server '$OP_CONNECT_SERVER' does not exist — its 1Password credentials are dead and cannot be reseeded. Run scripts/install/setup-secrets.sh first to recreate the server, then re-run with --reseed-secrets."
      confirm "Re-seed Connect secrets: re-apply op-credentials + onepassword-token from 1Password and restart Connect (release stays Flux-managed; no helm upgrade)." \
        || die "Re-seed declined; aborting."
      apply_connect_secrets
      restart_connect
      info "Re-seeded Connect secrets and restarted Connect; Flux still owns release '$CONNECT_RELEASE'."
    else
      info "Connect release '$CONNECT_RELEASE' already deployed (Flux-managed); skipping Helm seed."
      info "To re-apply rotated Connect secrets, re-run with --reseed-secrets."
    fi
  else
    # Recover a release left mid-operation by an interrupted run, so the
    # upgrade below does not fail with "another operation in progress" or
    # "has no deployed releases".
    case "$status" in
      pending-install)
        confirm "Connect release is stuck in 'pending-install'; uninstall the failed release before reinstalling." \
          && helm uninstall "$CONNECT_RELEASE" --namespace "$CONNECT_NS" --kube-context "$KUBE_CONTEXT" >/dev/null \
          || die "Cannot proceed over a stuck pending-install release."
        ;;
      pending-upgrade|pending-rollback|failed)
        warn "Connect release is in '$status' state."
        if confirm "Roll the Connect release back to its last deployed revision before upgrading."; then
          helm rollback "$CONNECT_RELEASE" --namespace "$CONNECT_NS" --kube-context "$KUBE_CONTEXT" >/dev/null 2>&1 \
            || helm uninstall "$CONNECT_RELEASE" --namespace "$CONNECT_NS" --kube-context "$KUBE_CONTEXT" >/dev/null
        else
          die "Cannot proceed over a '$status' release."
        fi
        ;;
    esac

    confirm "Seed 1Password Connect into namespace '$CONNECT_NS' (op-credentials + onepassword-token Secrets, then helm upgrade --install '$CONNECT_RELEASE')." \
      || die "Connect seed is required before Flux bootstrap; aborting."

    apply_connect_secrets

    helm repo add 1password https://1password.github.io/connect-helm-charts/ --force-update >/dev/null
    helm upgrade --install "$CONNECT_RELEASE" 1password/connect \
      --namespace "$CONNECT_NS" \
      --kube-context "$KUBE_CONTEXT" \
      --set connect.credentialsName=op-credentials \
      --set connect.credentialsKey=1password-credentials.json \
      --set operator.create=true \
      --set operator.token.name=onepassword-token \
      --set operator.token.key=token \
      --set pollingInterval=60
    info "Connect seeded; Flux will adopt release '$CONNECT_RELEASE'."
  fi
else
  step "1Password Connect"
  info "SKIP_ONEPASSWORD_BOOTSTRAP=1; skipping Connect seed."
fi

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
echo "Check reconciliation with:"
echo "  flux --context $KUBE_CONTEXT get all -A"
