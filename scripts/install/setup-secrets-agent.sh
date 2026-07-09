#!/usr/bin/env bash
# Idempotent secret setup for a single vicegerent agent, using Kubernetes Secrets
# directly (no 1Password). All material lives in the agent-sandbox namespace.
#
# Usage: setup-secrets-agent.sh <agent-name> [-y|--yes]
#
# Applies these Kubernetes Secrets in namespace agent-sandbox:
#   <name>-secrets                 password, signing-secret, public-key,
#                                  SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#                                  SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (Slack optional)
#   <name>-agentgateway-api-key    api-key         (random bearer token)
#   <name>-ssh-key                 hermes_agent_ed25519  (ed25519 private key)
#
# ALSO mirrors <name>-agentgateway-api-key into agentgateway-system/agentgateway-api-keys
# (keyed "<name>"), shared with setup-secrets-platform.sh's mcp-cerbos-shim entry via merge-patch.
#
# Generated material (dashboard auth, SSH key, bearer token) is generated once and
# reused on re-run; Slack values are taken from the environment or prompted for.
# Secrets are disposable/recreatable — keep your own copy of any Slack tokens.
#
# Flags:
#   -y, --yes     auto-approve every change (non-interactive)
#   -h, --help    show this help
#
# Env overrides: KUBE_CONTEXT, SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#   SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"
NS=agent-sandbox

ASSUME_YES=0
AGENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [[ -z "$AGENT" ]] && AGENT="$1" || { echo "unexpected argument: $1" >&2; exit 2; } ;;
  esac
  shift
done

[[ -n "$AGENT" ]] || { echo "usage: $0 <agent-name> [-y|--yes]" >&2; exit 2; }
AGENT="$(echo "$AGENT" | tr '[:upper:]' '[:lower:]')"

ITEM="${AGENT}-secrets"
ITEM_API_KEY="${AGENT}-agentgateway-api-key"  # pragma: allowlist secret
ITEM_SSH="${AGENT}-ssh-key"  # pragma: allowlist secret

# Fixed key name: the sandbox mounts this Secret at /opt/hermes-ssh/<name> and
# GIT_SSH_COMMAND in the chart references hermes_agent_ed25519.
SSH_KEY_FILE="hermes_agent_ed25519"

if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[0;33m'; R=$'\033[0;31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
info()  { echo "${G}•${N} $*"; }
step()  { echo; echo "${B}== $* ==${N}"; }
warn()  { echo "${Y}!${N} $*" >&2; }
die()   { echo "${R}ERROR:${N} $*" >&2; exit 1; }

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
ensure_ns() { kc create namespace "$1" --dry-run=client -o yaml | kc apply -f - >/dev/null; }
# secret_val <name> <key> — decoded value of a secret key (empty if absent).
secret_val() {
  local json b64
  json="$(kc -n "$NS" get secret "$1" -o json 2>/dev/null)" || return 0
  b64="$(printf '%s' "$json" | jq -r --arg k "$2" '.data[$k] // empty')"
  [[ -n "$b64" ]] && printf '%s' "$b64" | base64 -d
  return 0
}
# ensure_secret_key <name> <ns> <key> <value> — merge-patch ONE key into a
# Secret without touching any other key (shared with setup-secrets-platform.sh
# against agentgateway-api-keys; both scripts must be safe to re-run independently).
ensure_secret_key() {
  local name="$1" ns="$2" key="$3" value="$4"
  kc -n "$ns" get secret "$name" >/dev/null 2>&1 \
    || kc -n "$ns" create secret generic "$name" >/dev/null
  local b64; b64="$(printf '%s' "$value" | base64 | tr -d '\n')"
  kc -n "$ns" patch secret "$name" --type=merge \
    -p "{\"data\":{\"$key\":\"$b64\"}}" >/dev/null
}

# --- prerequisites ---------------------------------------------------------
for cmd in kubectl openssl ssh-keygen jq; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH"
done
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kubectl context '$KUBE_CONTEXT' does not exist"
current_ctx="$(kubectl config current-context 2>/dev/null || true)"
[[ "$current_ctx" == "$KUBE_CONTEXT" ]] \
  || die "current kubectl context is '${current_ctx:-<none>}', expected '$KUBE_CONTEXT' (run: kubectl config use-context $KUBE_CONTEXT)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-agent-setup.XXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

echo "${B}vicegerent agent secret setup${N}  (agent: $AGENT, context: $KUBE_CONTEXT)"
ensure_ns "$NS"

# --- agentgateway virtual API key ------------------------------------------
# Also mirrored into agentgateway-system/agentgateway-api-keys (keyed by
# $AGENT): that copy is what the apiKeyAuthentication policy validates
# against, this copy is what's mounted as AGENTGATEWAY_API_KEY. Merge-patched
# so other agents'/mcp-cerbos-shim's entries in the shared Secret survive.
step "$ITEM_API_KEY"
existing_key="$(secret_val "$ITEM_API_KEY" api-key)"
if [[ -n "$existing_key" ]]; then
  info "agentgateway API key already set; reusing."
  api_key="$existing_key"
else
  api_key="$(openssl rand -hex 32)"
  kc -n "$NS" create secret generic "$ITEM_API_KEY" \
    --from-literal="api-key=$api_key" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  info "Generated agentgateway API key."
fi
# Always (re-)mirror -- an upgrading install won't have the agentgateway-system
# copy yet; ensure_secret_key is idempotent.
ensure_secret_key agentgateway-api-keys agentgateway-system "$AGENT" "$api_key"

# --- SSH key ---------------------------------------------------------------
# ed25519 keypair (generate-once). Private key → <name>-ssh-key; public key is
# stored as the public-key field of <name>-secrets (assembled below).
step "$ITEM_SSH"
pubkey="$(secret_val "$ITEM" public-key || true)"
if [[ -n "$(secret_val "$ITEM_SSH" "$SSH_KEY_FILE")" ]]; then
  info "SSH key already present; reusing."
  [[ -n "$pubkey" ]] && { echo; echo "  ${Y}Public key${N} (add to GitLab/GitHub if not already):"; echo "  $pubkey"; }
else
  if confirm "Generate a new ed25519 SSH key for agent '$AGENT' ($ITEM_SSH)."; then
    ssh-keygen -t ed25519 -C "${AGENT}-agent@vicegerent" -N "" -f "$WORK/$SSH_KEY_FILE" >/dev/null 2>&1
    kc -n "$NS" create secret generic "$ITEM_SSH" \
      --from-file="$SSH_KEY_FILE=$WORK/$SSH_KEY_FILE" \
      --dry-run=client -o yaml | kc apply -f - >/dev/null
    pubkey="$(cat "$WORK/$SSH_KEY_FILE.pub")"
    info "Stored SSH private key in $ITEM_SSH."
    echo
    echo "  ${Y}Next step:${N} add the public key to your git hosts (GitLab/GitHub deploy keys):"
    echo "  $pubkey"
  else
    warn "SSH key generation skipped — git push/pull from the sandbox will not work until set."
  fi
fi

# --- agent secrets (dashboard auth + Slack + public key) -------------------
# Assembled and applied as a whole because `apply` replaces every key; existing
# generated values (password, signing-secret) and Slack fields are preserved.
step "$ITEM"
password="$(secret_val "$ITEM" password || true)"
signing="$(secret_val "$ITEM" signing-secret || true)"
[[ -z "$password" ]] && { password="$(openssl rand -base64 24 | tr -d '\n')"; info "Generated dashboard password."; }
[[ -z "$signing" ]] && { signing="$(openssl rand -base64 32 | tr -d '\n')"; info "Generated dashboard signing-secret."; }

args=(--from-literal="password=$password" --from-literal="signing-secret=$signing")
[[ -n "$pubkey" ]] && args+=(--from-literal="public-key=$pubkey")

# Slack fields (optional). env override > existing value > interactive prompt.
echo
echo "  Slack is optional. Create the app from a manifest:"
echo "    vicegerent slack manifest \"<BotName>\" | pbcopy"
echo "  then api.slack.com → Create New App → From manifest; enable Socket Mode; install to workspace."
for field in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL; do
  val="${!field:-}"
  [[ -z "$val" ]] && val="$(secret_val "$ITEM" "$field" || true)"
  if [[ -z "$val" && "$ASSUME_YES" != "1" ]]; then
    read -r -p "  $field (empty to skip): " val
  fi
  if [[ -n "$val" ]]; then
    args+=(--from-literal="$field=$val")
    info "$field set."
  fi
done

kc -n "$NS" create secret generic "$ITEM" "${args[@]}" --dry-run=client -o yaml | kc apply -f - >/dev/null
info "Applied $ITEM."

# --- verify ----------------------------------------------------------------
step "Verify"
missing=0
check() { if [[ -n "$(secret_val "$1" "$2")" ]]; then echo "  ${G}ok${N}   $NS/$1 ($2)"; else echo "  ${R}MISS${N} $NS/$1 ($2)"; missing=1; fi; }
check_optional() { if [[ -n "$(secret_val "$1" "$2")" ]]; then echo "  ${G}ok${N}   $NS/$1 ($2)"; else echo "  ${Y}skip${N} $NS/$1 ($2)  (optional)"; fi; }
# check_other_ns <name> <ns> <key> — like check, but for a Secret outside $NS
# (agentgateway-api-keys lives in agentgateway-system, not agent-sandbox).
check_other_ns() {
  local val; val="$(kc -n "$2" get secret "$1" -o json 2>/dev/null | jq -r --arg k "$3" '.data[$k] // empty')"
  if [[ -n "$val" ]]; then echo "  ${G}ok${N}   $2/$1 ($3)"; else echo "  ${R}MISS${N} $2/$1 ($3)"; missing=1; fi
}
check "$ITEM" password
check "$ITEM" signing-secret
check "$ITEM_API_KEY" api-key
check_other_ns agentgateway-api-keys agentgateway-system "$AGENT"
check_optional "$ITEM" public-key
check_optional "$ITEM_SSH" "$SSH_KEY_FILE"
check_optional "$ITEM" SLACK_BOT_TOKEN
check_optional "$ITEM" SLACK_APP_TOKEN
check_optional "$ITEM" SLACK_ALLOWED_USERS
check_optional "$ITEM" SLACK_HOME_CHANNEL

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material for agent '$AGENT' is present."
else
  warn "Some required material is missing (see above). Re-run to complete it."
  exit 1
fi
