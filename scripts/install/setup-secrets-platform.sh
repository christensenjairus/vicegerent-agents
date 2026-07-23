#!/usr/bin/env bash
# Idempotent secret setup for the vicegerent platform, using Kubernetes Secrets
# directly (no 1Password). The Kind cluster's etcd is the source of truth for cluster
# material; the host ghostunnel material lives on the laptop filesystem.
#
# Secrets are treated as disposable/recreatable: re-run this script to reseed a
# fresh cluster. Generated material (CAs, certs, random keys) is reused when it
# already exists and regenerated when absent (or with --force); user-supplied API
# keys are read from the environment or prompted for, and you are expected to keep
# your own copy of them elsewhere.
#
# Applies these Kubernetes Secrets (and one ConfigMap):
#   agentgateway-system  vicegerent-anthropic-secrets Authorization        (Anthropic API key)
#   agentgateway-system  vicegerent-openai-secrets    Authorization        (OpenAI API key, optional)
#   agentgateway-system  vicegerent-deepseek-secrets  Authorization        (DeepSeek API key, optional)
#   agentgateway-system  vicegerent-zai-secrets       Authorization        (Z.ai/GLM API key, optional)
#   agentgateway-system  vicegerent-mcp-client        tls.crt, tls.key     (ghostunnel client cert)
#   agentgateway-system  ghostunnel-ca (ConfigMap)    ca.crt               (ghostunnel CA cert)
#   agentgateway-system  ghostunnel-server            server.crt/key,ca.crt (host recovery copy)
#   searxng              searxng-secret               secret_key           (generated)
#   egress-proxy         egress-proxy-ca              ca.crt, ca.key       (MITM proxy CA)
#   agent-sandbox        egress-proxy-ca-cert         ca.crt               (MITM proxy CA cert only)
#   velero               velero-credentials           cloud                (generated S3 creds)
#
# MCP server API keys (tavily/firecrawl/gitlab) are NOT Kubernetes Secrets — those
# servers run host-side under ToolHive and read their keys from `thv` secrets.
#
# The host ghostunnel material (ca.cert, ca.key, server.crt, server.key,
# client.crt, client.key) is written to $GHOSTUNNEL_HOST_DIR (default
# ~/.vicegerent/ghostunnel). The server key never enters Kubernetes; the CA key
# stays host-only so leaf certs can be re-issued without rebuilding the chain.
#
# The Velero S3 credentials are generated once and mirrored to both the
# velero/velero-credentials Secret and $RCLONE_S3_HOST_DIR/auth-key (default
# ~/.vicegerent/rclone-s3), read by the host rclone serve s3 (host/mcp/vicegerent_mcp.py).
#
# Flags:
#   -y, --yes     auto-approve every change (non-interactive)
#   --force       rebuild the ghostunnel CA + leaf certs even if they already exist
#   -h, --help    show this help
#
# Env overrides: KUBE_CONTEXT, GHOSTUNNEL_HOST_DIR, RCLONE_S3_HOST_DIR, SERVER_IP,
#   SERVER_CN, CLIENT_CN, ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY,
#   ZAI_API_KEY, TAVILY_API_KEY, FIRECRAWL_API_KEY,
#   GITLAB_PERSONAL_ACCESS_TOKEN

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-vicegerent}"
GHOSTUNNEL_HOST_DIR="${GHOSTUNNEL_HOST_DIR:-$HOME/.vicegerent/ghostunnel}"
RCLONE_S3_HOST_DIR="${RCLONE_S3_HOST_DIR:-$HOME/.vicegerent/rclone-s3}"

# The cluster reaches the host ghostunnel server via host.docker.internal; the
# server cert's SAN must match it. SERVER_IP is the loopback SAN for local tests.
SERVER_IP="${SERVER_IP:-127.0.0.1}"
SERVER_CN="${SERVER_CN:-host.docker.internal}"
CLIENT_CN="${CLIENT_CN:-agent-client}"

ASSUME_YES=0
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

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

# secret_b64 <name> <ns> <key> — base64 value of a secret key (empty if absent).
secret_b64() {
  local json
  json="$(kc -n "$2" get secret "$1" -o json 2>/dev/null)" || return 0
  printf '%s' "$json" | jq -r --arg k "$3" '.data[$k] // empty'
}
secret_has() { [[ -n "$(secret_b64 "$1" "$2" "$3")" ]]; }

# apply_secret <name> <ns> <create-args...> — create/update a Secret idempotently.
apply_secret() {
  local name="$1" ns="$2"; shift 2
  kc -n "$ns" create secret generic "$name" "$@" --dry-run=client -o yaml | kc apply -f - >/dev/null
}

# ensure_literal_secret <name> <ns> <key> <envvar> <prompt> <required(0|1)>
# Reuses an existing non-empty value; otherwise takes it from $envvar or a prompt,
# then applies the Secret (empty value allowed so the resource always exists).
ensure_literal_secret() {
  local name="$1" ns="$2" key="$3" envvar="$4" prompt="$5" required="$6"
  if secret_has "$name" "$ns" "$key" && [[ -z "${!envvar:-}" ]]; then
    info "$ns/$name ($key) already set; reusing."
    return 0
  fi
  local val="${!envvar:-}"
  if [[ -z "$val" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      warn "No $envvar in environment and --yes is set; leaving $ns/$name ($key) empty."
    else
      echo
      echo "${Y}CHANGE:${N} Set $ns/$name ($key). $prompt"
      read -r -s -p "  value (empty to skip): " val; echo
    fi
  fi
  apply_secret "$name" "$ns" --from-literal="$key=$val"
  if [[ -n "$val" ]]; then
    info "Set $ns/$name ($key)."
  elif [[ "$required" == "1" ]]; then
    warn "$ns/$name ($key) left empty — set it before this credential will work."
  else
    warn "$ns/$name ($key) left empty (optional)."
  fi
  unset val
}

# ensure_velero_credentials — generate-once S3 creds, mirrored to the k8s Secret and host auth-key file.
ensure_velero_credentials() {
  local hostfile="$RCLONE_S3_HOST_DIR/auth-key" access="" secret=""
  if [[ -s "$hostfile" ]]; then
    IFS=',' read -r access secret < "$hostfile"
    info "Reusing Velero S3 credentials from $hostfile."
  elif secret_has velero-credentials velero cloud; then
    local cloud
    cloud="$(secret_b64 velero-credentials velero cloud | base64 -d)"
    access="$(printf '%s\n' "$cloud" | sed -n 's/^aws_access_key_id=//p')"
    secret="$(printf '%s\n' "$cloud" | sed -n 's/^aws_secret_access_key=//p')"
    info "Recovered Velero S3 credentials from velero/velero-credentials."
  fi
  if [[ -z "$access" || -z "$secret" ]]; then
    access="$(openssl rand -hex 16)"
    secret="$(openssl rand -hex 32)"
    info "Generated new Velero S3 credentials."
  fi
  apply_secret velero-credentials velero \
    --from-literal="cloud=$(printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' "$access" "$secret")"
  mkdir -p "$RCLONE_S3_HOST_DIR"
  chmod 700 "$RCLONE_S3_HOST_DIR"
  printf '%s,%s\n' "$access" "$secret" > "$hostfile"
  chmod 600 "$hostfile"
  info "Applied velero/velero-credentials Secret + host auth-key ($hostfile)."
}

# --- prerequisites ---------------------------------------------------------
for cmd in kubectl openssl jq; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH"
done
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kubectl context '$KUBE_CONTEXT' does not exist"
current_ctx="$(kubectl config current-context 2>/dev/null || true)"
[[ "$current_ctx" == "$KUBE_CONTEXT" ]] \
  || die "current kubectl context is '${current_ctx:-<none>}', expected '$KUBE_CONTEXT' (run: kubectl config use-context $KUBE_CONTEXT)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-setup.XXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

echo "${B}vicegerent platform secret setup${N}  (context: $KUBE_CONTEXT)"
[[ "$FORCE" == "1" ]] && warn "--force: the ghostunnel CA and all leaf certificates will be rebuilt."

# Ensure target namespaces exist so Secrets can be applied before Flux creates them.
step "Namespaces"
for ns in agentgateway-system searxng egress-proxy agent-sandbox velero; do
  ensure_ns "$ns"
done
info "Target namespaces present."

# --- ghostunnel mTLS -------------------------------------------------------
# Host-only CA + server cert (laptop) and a client cert (cluster). The cluster
# never sees the server or CA private key.
step "Ghostunnel mTLS material"
mkdir -p "$GHOSTUNNEL_HOST_DIR"
chmod 700 "$GHOSTUNNEL_HOST_DIR"
HD="$GHOSTUNNEL_HOST_DIR"

new_ca=0
if [[ "$FORCE" == "1" ]]; then
  confirm "Rebuild the ghostunnel CA in $HD (invalidates any previously issued certs)." || die "Aborted."
  new_ca=1
elif [[ -s "$HD/ca.cert" && -s "$HD/ca.key" ]]; then
  info "Ghostunnel CA already in $HD; reusing."
else
  confirm "Generate a ghostunnel CA (4096-bit, 10y) in $HD (private key stays host-only)." || die "CA is required; aborting."
  new_ca=1
fi

if [[ $new_ca -eq 1 ]]; then
  openssl genrsa -out "$HD/ca.key" 4096 >/dev/null 2>&1
  # keyUsage=keyCertSign is required: OpenSSL 3 / Python reject a CA without it.
  openssl req -x509 -new -nodes -key "$HD/ca.key" -sha256 -days 3650 \
    -subj "/CN=vicegerent-ghostunnel-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$HD/ca.cert" >/dev/null 2>&1
  info "Generated a new ghostunnel CA."
fi

if [[ $new_ca -eq 1 || ! -s "$HD/server.crt" || ! -s "$HD/server.key" ]]; then
  openssl genrsa -out "$HD/server.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$HD/server.key" -subj "/CN=${SERVER_CN}" -out "$WORK/server.csr" >/dev/null 2>&1
  printf 'subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n' "$SERVER_CN" "$SERVER_IP" > "$WORK/server.ext"
  openssl x509 -req -in "$WORK/server.csr" -CA "$HD/ca.cert" -CAkey "$HD/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$WORK/server.ext" -out "$HD/server.crt" >/dev/null 2>&1
  info "Issued ghostunnel server certificate (host-only)."
else
  info "Ghostunnel server certificate already present; reusing."
fi

if [[ $new_ca -eq 1 || ! -s "$HD/client.crt" || ! -s "$HD/client.key" ]]; then
  openssl genrsa -out "$HD/client.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$HD/client.key" -subj "/CN=${CLIENT_CN}" -out "$WORK/client.csr" >/dev/null 2>&1
  printf 'extendedKeyUsage=clientAuth\n' > "$WORK/client.ext"
  openssl x509 -req -in "$WORK/client.csr" -CA "$HD/ca.cert" -CAkey "$HD/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$WORK/client.ext" -out "$HD/client.crt" >/dev/null 2>&1
  info "Issued ghostunnel client certificate."
else
  info "Ghostunnel client certificate already present; reusing."
fi
chmod 600 "$HD"/* 2>/dev/null || true

# The cluster gets the client identity as a Secret and the CA cert as a ConfigMap
# (agentgateway caCertificateRefs resolves to a ConfigMap keyed ca.crt).
apply_secret vicegerent-mcp-client agentgateway-system \
  --from-file=tls.crt="$HD/client.crt" --from-file=tls.key="$HD/client.key"
kc -n agentgateway-system create configmap ghostunnel-ca \
  --from-file=ca.crt="$HD/ca.cert" --dry-run=client -o yaml | kc apply -f - >/dev/null
info "Applied vicegerent-mcp-client Secret + ghostunnel-ca ConfigMap."

# Mirror the ghostunnel SERVER material (cert + key + CA cert) into the cluster so a
# host missing ~/.vicegerent/ghostunnel can recover it before ghostunnel starts
# (vicegerent-mcp start -> ensure_ghostunnel_material). The CA *key* is NOT mirrored —
# it only signs new certs, so a full rebuild still means re-running this script.
apply_secret ghostunnel-server agentgateway-system \
  --from-file=server.crt="$HD/server.crt" --from-file=server.key="$HD/server.key" \
  --from-file=ca.crt="$HD/ca.cert"
info "Applied ghostunnel-server Secret (server cert/key + CA for host recovery)."

# --- model API keys --------------------------------------------------------
step "Anthropic API key"
ensure_literal_secret vicegerent-anthropic-secrets agentgateway-system Authorization \
  ANTHROPIC_API_KEY "Anthropic API key (sk-ant-...)." 1

step "OpenAI API key (optional)"
ensure_literal_secret vicegerent-openai-secrets agentgateway-system Authorization \
  OPENAI_API_KEY "OpenAI API key (sk-...) — GPT models stay unavailable until set." 0

step "DeepSeek API key (optional)"
ensure_literal_secret vicegerent-deepseek-secrets agentgateway-system Authorization \
  DEEPSEEK_API_KEY "DeepSeek API key — DeepSeek models stay unavailable until set and providers.deepseek.enabled is true." 0

step "Z.ai / GLM API key (optional)"
ensure_literal_secret vicegerent-zai-secrets agentgateway-system Authorization \
  ZAI_API_KEY "Z.ai/GLM standard (metered) API key — Z.ai models stay unavailable until set and providers.zai.enabled is true." 0

# --- SearXNG secret key ----------------------------------------------------
# Signs SearXNG session/limiter tokens. Generated once and reused so the value
# stays stable across restarts (a changed key breaks limiter tokens and cache).
step "SearXNG secret key"
if secret_has searxng-secret searxng secret_key; then
  info "searxng/searxng-secret (secret_key) already set; reusing."
else
  confirm "Generate a SearXNG secret key (searxng/searxng-secret)." || die "SearXNG secret key is required; aborting."
  apply_secret searxng-secret searxng --from-literal="secret_key=$(openssl rand -hex 32)"
  info "Generated searxng/searxng-secret."
fi

# --- egress proxy CA -------------------------------------------------------
# Dedicated CA for the MITM egress proxy (generate-once). The private key lives
# only in the egress-proxy Secret; agent-sandbox gets the cert only.
step "Egress proxy CA"
if secret_has egress-proxy-ca egress-proxy ca.key && [[ "$FORCE" != "1" ]]; then
  info "egress-proxy/egress-proxy-ca already present; reusing."
  secret_b64 egress-proxy-ca egress-proxy ca.crt | base64 -d > "$WORK/proxy-ca.crt"
else
  confirm "Generate a new CA for the egress MITM proxy (egress-proxy/egress-proxy-ca)." \
    || die "Egress proxy CA is required for sandbox outbound HTTPS; aborting."
  openssl genrsa -out "$WORK/proxy-ca.key" 4096 >/dev/null 2>&1
  openssl req -x509 -new -nodes -key "$WORK/proxy-ca.key" -sha256 -days 3650 \
    -subj "/CN=vicegerent-egress-proxy-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$WORK/proxy-ca.crt" >/dev/null 2>&1
  apply_secret egress-proxy-ca egress-proxy \
    --from-file=ca.crt="$WORK/proxy-ca.crt" --from-file=ca.key="$WORK/proxy-ca.key"
  info "Generated egress proxy CA (egress-proxy/egress-proxy-ca)."
fi
# The cert-only copy consumed by agent-sandbox (idempotent — public material).
apply_secret egress-proxy-ca-cert agent-sandbox --from-file=ca.crt="$WORK/proxy-ca.crt"
info "Applied agent-sandbox/egress-proxy-ca-cert (cert only)."

# --- Velero S3 credentials -------------------------------------------------
step "Velero S3 credentials"
ensure_velero_credentials

# --- MCP credentials -------------------------------------------------------
# The tavily/firecrawl/gitlab MCP servers now run host-side under ToolHive, so
# their API keys are `thv` secrets on the laptop (see scripts/host/setup-host-mcp),
# not Kubernetes Secrets. Nothing MCP-credential-related is applied to the cluster.

# --- verify ----------------------------------------------------------------
step "Verify"
missing=0
check() {
  if secret_has "$1" "$2" "$3"; then echo "  ${G}ok${N}   $2/$1 ($3)"; else echo "  ${R}MISS${N} $2/$1 ($3)"; missing=1; fi
}
check_optional() {
  if secret_has "$1" "$2" "$3"; then echo "  ${G}ok${N}   $2/$1 ($3)"; else echo "  ${Y}empty${N} $2/$1 ($3)  (optional)"; fi
}
check vicegerent-mcp-client agentgateway-system tls.crt
check vicegerent-mcp-client agentgateway-system tls.key
check ghostunnel-server agentgateway-system server.crt
check ghostunnel-server agentgateway-system server.key
check_optional vicegerent-anthropic-secrets agentgateway-system Authorization
check searxng-secret searxng secret_key
check egress-proxy-ca egress-proxy ca.crt
check egress-proxy-ca egress-proxy ca.key
check egress-proxy-ca-cert agent-sandbox ca.crt
check velero-credentials velero cloud
check_optional vicegerent-openai-secrets agentgateway-system Authorization
check_optional vicegerent-deepseek-secrets agentgateway-system Authorization
check_optional vicegerent-zai-secrets agentgateway-system Authorization
if [[ -s "$HD/server.crt" && -s "$HD/server.key" && -s "$HD/ca.cert" ]]; then
  echo "  ${G}ok${N}   $HD (host ghostunnel server + CA material)"
else
  echo "  ${R}MISS${N} $HD (host ghostunnel material)"; missing=1
fi
if [[ -s "$RCLONE_S3_HOST_DIR/auth-key" ]]; then
  echo "  ${G}ok${N}   $RCLONE_S3_HOST_DIR/auth-key (host rclone S3 auth-key)"
else
  echo "  ${R}MISS${N} $RCLONE_S3_HOST_DIR/auth-key (host rclone S3 auth-key)"; missing=1
fi

echo
if [[ $missing -eq 0 ]]; then
  info "All required platform secret material is present."
else
  warn "Some required material is missing (see above). Re-run to complete it."
  exit 1
fi
