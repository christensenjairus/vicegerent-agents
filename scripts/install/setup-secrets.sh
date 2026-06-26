#!/usr/bin/env bash
# Idempotent 1Password + ghostunnel setup for the vicegerent platform.
#
# Provisions, in 1Password vault "Vicegerent":
#   - the vault itself
#   - a Connect server + its credentials file (item "Connect Credentials")
#   - a Connect operator token (item "Connect Token")
#   - ghostunnel mTLS certificates split across three items:
#       Runtime        tls.crt, tls.key, Authorization   (synced into the cluster)
#       MCP CA         ca.cert                            (synced into the cluster)
#       Ghostunnel Host  server.crt, server.key, ca.cert, ca.key  (host-only)
#       Dashboard Tunnel server.crt, server.key (cluster) + client.crt, client.key (host) + ca.cert
#   - a SearXNG secret key (item "SearXNG") synced into the cluster
#   - Graphiti tribal-knowledge secrets: a FalkorDB password (item
#     "GraphitiFalkorDB", field "password") and a gateway virtual key (item
#     "GraphitiGatewayKey", field "api-key"), both synced into the cluster
#
# Properties:
#   - Idempotent: anything already present in 1Password is reused, never regenerated.
#     The CA private key is kept in "Ghostunnel Host" so a missing leaf cert can be
#     re-issued from the existing CA without rebuilding the whole chain.
#   - Self-cleaning: all key material is written only inside a private tmpdir that is
#     removed on any exit (including Ctrl-C). Nothing is left on disk.
#   - Verbose: every mutating step is announced and confirmed before it runs.
#     Steps that change nothing run silently (just an "already present" note).
#
# Flags:
#   -y, --yes     auto-approve every change (non-interactive)
#   --force       rebuild the entire CA + leaf certs even if they already exist
#   -h, --help    show this help

set -euo pipefail

VAULT="${VAULT:-Vicegerent}"
SERVER_NAME="${OP_CONNECT_SERVER:-Vicegerent}"
TOKEN_NAME="${OP_CONNECT_TOKEN_NAME:-Vicegerent Operator}"

RUNTIME_ITEM="Runtime"
CA_ITEM="MCP CA"
HOST_ITEM="Ghostunnel Host"
DASHBOARD_TUNNEL_ITEM="Dashboard Tunnel"
CRED_ITEM="Connect Credentials"
OPENAI_ITEM="OpenAI"
SEARXNG_ITEM="SearXNG"
TAVILY_ITEM="Tavily"
FIRECRAWL_ITEM="Firecrawl"
GRAPHITI_FALKORDB_ITEM="GraphitiFalkorDB"
GRAPHITI_GATEWAY_KEY_ITEM="GraphitiGatewayKey"
TOKEN_ITEM="Connect Token"

HOST_ONLY_IP="${HOST_ONLY_IP:-192.168.64.1}"
SERVER_CN="${SERVER_CN:-host.minikube.internal}"
CLIENT_CN="${CLIENT_CN:-agent-client}"
# Dashboard tunnel runs the OTHER direction: the ghostunnel server lives in the
# agent pod and the laptop runs the client. The server cert's SAN must match the
# address Hermes Desktop's host-side client dials — the minikube node IP, which
# the NodePort Service exposes on the host-only network. Auto-detected from the
# vicegerent minikube profile; override with DASHBOARD_TUNNEL_IP=... if needed.
# The driver determines the range (vfkit/qemu → 192.168.64.x, docker → 192.168.49.x),
# so never hardcode it.
DASHBOARD_TUNNEL_SERVER_CN="${DASHBOARD_TUNNEL_SERVER_CN:-hermes-dashboard-tunnel}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-vicegerent}"
DASHBOARD_TUNNEL_IP="${DASHBOARD_TUNNEL_IP:-$(minikube -p "$MINIKUBE_PROFILE" ip 2>/dev/null || true)}"
DASHBOARD_TUNNEL_CLIENT_CN="${DASHBOARD_TUNNEL_CLIENT_CN:-dashboard-client}"

# Leaf certs are issued for 825 days. Warn and offer to re-issue once a stored
# leaf has less than this many days of validity left, so the chain never lapses.
EXPIRY_THRESHOLD_DAYS="${EXPIRY_THRESHOLD_DAYS:-180}"

ASSUME_YES=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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

# Announce a pending change and ask before doing it. Returns 0 to proceed.
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

op_item_exists()  { op item get "$1" --vault "$VAULT" >/dev/null 2>&1; }
op_field_exists() { op read "op://$VAULT/$1/$2" >/dev/null 2>&1; }

# leaf_expiring_soon <item> <crt-field> — true (0) when the stored cert expires
# within EXPIRY_THRESHOLD_DAYS. Returns 1 (false) when it is valid beyond the
# threshold or cannot be read (a read failure is handled by the reuse branch).
leaf_expiring_soon() {
  local item="$1" field="$2" tmp
  tmp="$(mktemp "$CERTS/expiry.XXXXXX")"
  if ! op read "op://$VAULT/$item/$field" >"$tmp" 2>/dev/null; then
    rm -f "$tmp"; return 1
  fi
  local secs=$(( EXPIRY_THRESHOLD_DAYS * 86400 ))
  if openssl x509 -checkend "$secs" -noout -in "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; return 1   # valid beyond the threshold
  fi
  rm -f "$tmp"; return 0     # expires within the threshold
}

# cert_missing_ip <item> <crt-field> <ip> — true (0) when the stored cert does
# NOT carry <ip> in its SAN (so it must be re-issued for the current node IP).
# Returns 1 (false) when the IP is present, the cert can't be read, or no IP was
# given (nothing to check).
cert_missing_ip() {
  local item="$1" field="$2" ip="$3" tmp
  [[ -z "$ip" ]] && return 1
  tmp="$(mktemp "$CERTS/sanchk.XXXXXX")"
  if ! op read "op://$VAULT/$item/$field" >"$tmp" 2>/dev/null; then
    rm -f "$tmp"; return 1
  fi
  if openssl x509 -in "$tmp" -noout -text 2>/dev/null | grep -qF "IP Address:$ip"; then
    rm -f "$tmp"; return 1   # IP present
  fi
  rm -f "$tmp"; return 0     # IP missing → re-issue
}

ensure_item() {
  local title="$1"
  if ! op_item_exists "$title"; then
    op item create --category "Secure Note" --vault "$VAULT" --title "$title" >/dev/null
  fi
}

# set_field <item> <field> <file> [concealed|text]
set_field() {
  local title="$1" field="$2" file="$3" type="${4:-concealed}"
  local esc="${field//./\\.}"
  op item edit "$title" --vault "$VAULT" "${esc}[${type}]=$(cat "$file")" >/dev/null
}

connect_server_exists() {
  op connect server list --format json 2>/dev/null \
    | jq -e --arg n "$SERVER_NAME" '.[]? | select(.name==$n)' >/dev/null 2>&1
}

# --- prerequisites ---------------------------------------------------------
for cmd in op openssl jq; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH"
done
op account get >/dev/null 2>&1 || die "1Password CLI is not signed in. Run: op signin"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-setup.XXXXXX")"
chmod 700 "$WORK"
CERTS="$WORK/certs"
mkdir -p "$CERTS"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

echo "${B}vicegerent secret setup${N}  (vault: $VAULT)"
echo "Scratch dir: $WORK  — removed automatically on exit."
[[ "$FORCE" == "1" ]] && warn "--force: the CA and all leaf certificates will be rebuilt."

# --- vault -----------------------------------------------------------------
step "1Password vault"
if op vault get "$VAULT" >/dev/null 2>&1; then
  info "Vault '$VAULT' already exists; nothing to do."
else
  confirm "Create 1Password vault '$VAULT' (Connect cannot use Personal/Private/Employee vaults)." \
    || die "Vault is required; aborting."
  op vault create "$VAULT" >/dev/null
  info "Created vault '$VAULT'."
fi

# --- Connect server + credentials ------------------------------------------
step "1Password Connect credentials"
creds_present=0
op_item_exists "$CRED_ITEM" && op_field_exists "$CRED_ITEM" "1password-credentials.json" && creds_present=1

if [[ $creds_present -eq 1 ]] && connect_server_exists; then
  info "Connect credentials already in 1Password ('$CRED_ITEM') and server '$SERVER_NAME' exists; nothing to do."
else
  if [[ $creds_present -eq 1 ]]; then
    # Creds in 1Password but the Connect server is gone. A deleted server cannot
    # be reattached: its credentials file and every token it signed are dead.
    # Discard both so they are reissued against a fresh server below.
    warn "Connect credentials exist in '$CRED_ITEM' but no Connect server named '$SERVER_NAME' exists."
    warn "A deleted Connect server cannot be reattached; the stored credentials and token are unusable."
    confirm "Delete the stale '$CRED_ITEM' + '$TOKEN_ITEM' items and create a fresh Connect server." \
      || die "Cannot proceed with credentials for a Connect server that no longer exists."
    op item delete "$CRED_ITEM" --vault "$VAULT" >/dev/null 2>&1 || true
    op item delete "$TOKEN_ITEM" --vault "$VAULT" >/dev/null 2>&1 || true
  elif connect_server_exists; then
    warn "A Connect server named '$SERVER_NAME' exists, but its credentials are NOT in 1Password."
    warn "The credentials file is emitted only at creation time and cannot be recovered."
    confirm "Delete the orphaned Connect server '$SERVER_NAME' and recreate it." \
      || die "Cannot proceed without usable Connect credentials."
    op connect server delete "$SERVER_NAME" >/dev/null
  fi
  confirm "Create Connect server '$SERVER_NAME' for vault '$VAULT' and store its credentials as item '$CRED_ITEM'." \
    || die "Connect server is required; aborting."
  ( cd "$WORK" && op connect server create "$SERVER_NAME" --vaults "$VAULT" >/dev/null )
  [[ -s "$WORK/1password-credentials.json" ]] || die "Connect server created but no credentials file was emitted."
  ensure_item "$CRED_ITEM"
  set_field "$CRED_ITEM" "1password-credentials.json" "$WORK/1password-credentials.json" concealed
  info "Stored Connect credentials in '$CRED_ITEM'."
fi

# --- Connect token ---------------------------------------------------------
step "1Password Connect token"
if op_item_exists "$TOKEN_ITEM" && op_field_exists "$TOKEN_ITEM" "token"; then
  info "Connect token already in 1Password ('$TOKEN_ITEM'); nothing to do."
else
  confirm "Create a Connect operator token and store it as item '$TOKEN_ITEM'." \
    || die "Connect token is required; aborting."
  TOKEN="$(op connect token create "$TOKEN_NAME" --server "$SERVER_NAME" --vault "$VAULT")"
  [[ -n "$TOKEN" ]] || die "Connect token creation returned empty (does the server exist?)."
  ensure_item "$TOKEN_ITEM"
  op item edit "$TOKEN_ITEM" --vault "$VAULT" "token[concealed]=$TOKEN" >/dev/null
  unset TOKEN
  info "Stored Connect token in '$TOKEN_ITEM'."
fi

# --- certificate authority -------------------------------------------------
step "Ghostunnel mTLS certificate authority"
have_ca_cert=0; have_ca_key=0
op_field_exists "$HOST_ITEM" "ca.cert" && have_ca_cert=1
op_field_exists "$HOST_ITEM" "ca.key"  && have_ca_key=1

# Detect an unrecoverable split: leaf certs exist but the CA key to re-sign is gone.
leaf_present=0
op_field_exists "$RUNTIME_ITEM" "tls.crt" && leaf_present=1
op_field_exists "$HOST_ITEM" "server.crt" && leaf_present=1

NEW_CA=0
if [[ "$FORCE" == "1" ]]; then
  confirm "Rebuild the ghostunnel CA from scratch (invalidates any previously issued certs)." \
    || die "Aborted."
  NEW_CA=1
elif [[ $have_ca_cert -eq 1 && $have_ca_key -eq 1 ]]; then
  info "CA already in 1Password ('$HOST_ITEM' ca.cert + ca.key); reusing it."
  op read "op://$VAULT/$HOST_ITEM/ca.cert" > "$CERTS/ca.crt"
  op read "op://$VAULT/$HOST_ITEM/ca.key"  > "$CERTS/ca.key"
elif [[ $leaf_present -eq 1 ]]; then
  die "Leaf certificates exist but the CA private key is missing from op://$VAULT/$HOST_ITEM/ca.key.
The CA cannot be reconstructed, so certs cannot be re-issued idempotently.
Rerun with --force to rebuild the entire chain (this regenerates all certs)."
else
  confirm "Generate a new ghostunnel CA (4096-bit, 10y); the private key is stored host-only in '$HOST_ITEM'." \
    || die "CA is required; aborting."
  NEW_CA=1
fi

if [[ $NEW_CA -eq 1 ]]; then
  openssl genrsa -out "$CERTS/ca.key" 4096 >/dev/null 2>&1
  openssl req -x509 -new -nodes -key "$CERTS/ca.key" -sha256 -days 3650 \
    -subj "/CN=vicegerent-ghostunnel-ca" -out "$CERTS/ca.crt" >/dev/null 2>&1
  info "Generated a new CA."
fi

# Decide which leaves to issue. A new CA forces both leaves to be re-issued.
need_server=0; need_client=0
if [[ $NEW_CA -eq 1 ]]; then
  need_server=1; need_client=1
else
  if op_field_exists "$HOST_ITEM" "server.crt" && op_field_exists "$HOST_ITEM" "server.key"; then
    if leaf_expiring_soon "$HOST_ITEM" "server.crt"; then
      warn "Server certificate expires within ${EXPIRY_THRESHOLD_DAYS} days."
      if confirm "Re-issue the server cert from the existing CA (resets validity to 825 days)."; then
        need_server=1
      else
        info "Keeping the existing server certificate."
      fi
    else
      info "Server certificate already present; reusing it."
    fi
  else
    need_server=1
  fi
  if op_field_exists "$RUNTIME_ITEM" "tls.crt" && op_field_exists "$RUNTIME_ITEM" "tls.key"; then
    if leaf_expiring_soon "$RUNTIME_ITEM" "tls.crt"; then
      warn "Client certificate expires within ${EXPIRY_THRESHOLD_DAYS} days."
      if confirm "Re-issue the client cert from the existing CA (resets validity to 825 days)."; then
        need_client=1
      else
        info "Keeping the existing client certificate."
      fi
    else
      info "Client certificate already present; reusing it."
    fi
  else
    need_client=1
  fi
fi

# --- server certificate ----------------------------------------------------
if [[ $need_server -eq 1 ]]; then
  step "Server certificate"
  confirm "Issue a server cert for CN=$SERVER_CN (SAN: DNS:$SERVER_CN, IP:$HOST_ONLY_IP)." \
    || die "Aborted."
  openssl genrsa -out "$CERTS/server.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$CERTS/server.key" -subj "/CN=${SERVER_CN}" -out "$CERTS/server.csr" >/dev/null 2>&1
  printf 'subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n' "$SERVER_CN" "$HOST_ONLY_IP" > "$CERTS/server.ext"
  openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$CERTS/server.ext" -out "$CERTS/server.crt" >/dev/null 2>&1
  info "Issued server certificate."
fi

# --- client certificate ----------------------------------------------------
if [[ $need_client -eq 1 ]]; then
  step "Client certificate"
  confirm "Issue a client cert for CN=$CLIENT_CN (this is the --allow-cn the host ghostunnel enforces)." \
    || die "Aborted."
  openssl genrsa -out "$CERTS/client.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$CERTS/client.key" -subj "/CN=${CLIENT_CN}" -out "$CERTS/client.csr" >/dev/null 2>&1
  printf 'extendedKeyUsage=clientAuth\n' > "$CERTS/client.ext"
  openssl x509 -req -in "$CERTS/client.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$CERTS/client.ext" -out "$CERTS/client.crt" >/dev/null 2>&1
  info "Issued client certificate."
fi

# --- populate items --------------------------------------------------------
step "Populate 1Password items"

if [[ $NEW_CA -eq 1 ]] || ! op_field_exists "$CA_ITEM" "ca.cert"; then
  ensure_item "$CA_ITEM"
  set_field "$CA_ITEM" "ca.cert" "$CERTS/ca.crt" text
  info "Set '$CA_ITEM' ca.cert (public CA, synced into the cluster)."
fi

if [[ $NEW_CA -eq 1 ]] || [[ $have_ca_key -eq 0 ]]; then
  ensure_item "$HOST_ITEM"
  set_field "$HOST_ITEM" "ca.cert" "$CERTS/ca.crt" text
  set_field "$HOST_ITEM" "ca.key"  "$CERTS/ca.key" concealed
  info "Set '$HOST_ITEM' ca.cert + ca.key (CA authority, host-only)."
fi

if [[ $need_server -eq 1 ]]; then
  ensure_item "$HOST_ITEM"
  set_field "$HOST_ITEM" "server.crt" "$CERTS/server.crt" text
  set_field "$HOST_ITEM" "server.key" "$CERTS/server.key" concealed
  info "Set '$HOST_ITEM' server.crt + server.key (host-only)."
fi

if [[ $need_client -eq 1 ]]; then
  ensure_item "$RUNTIME_ITEM"
  set_field "$RUNTIME_ITEM" "tls.crt" "$CERTS/client.crt" text
  set_field "$RUNTIME_ITEM" "tls.key" "$CERTS/client.key" concealed
  info "Set '$RUNTIME_ITEM' tls.crt + tls.key (client identity, synced into the cluster)."
fi

# --- dashboard tunnel certificates -----------------------------------------
# Separate mTLS leaf pair for the dashboard tunnel (pod-side ghostunnel server +
# laptop-side client), signed by the SAME CA. Issued only when missing or near
# expiry, and always re-issued when the CA was just rebuilt. The server cert
# lands in the cluster (pod sidecar); the client cert is pulled to the host.
step "Dashboard tunnel certificates"
need_dash_server=0; need_dash_client=0
if [[ $NEW_CA -eq 1 ]]; then
  need_dash_server=1; need_dash_client=1
else
  if op_field_exists "$DASHBOARD_TUNNEL_ITEM" "server.crt" && op_field_exists "$DASHBOARD_TUNNEL_ITEM" "server.key"; then
    if cert_missing_ip "$DASHBOARD_TUNNEL_ITEM" "server.crt" "$DASHBOARD_TUNNEL_IP"; then
      warn "Dashboard tunnel server certificate SAN does not include the current node IP ${DASHBOARD_TUNNEL_IP}; re-issuing."
      need_dash_server=1
    elif leaf_expiring_soon "$DASHBOARD_TUNNEL_ITEM" "server.crt"; then
      warn "Dashboard tunnel server certificate expires within ${EXPIRY_THRESHOLD_DAYS} days."
      confirm "Re-issue the dashboard tunnel server cert from the existing CA (resets validity to 825 days)." \
        && need_dash_server=1 || info "Keeping the existing dashboard tunnel server certificate."
    else
      info "Dashboard tunnel server certificate already present (SAN covers ${DASHBOARD_TUNNEL_IP}); reusing it."
    fi
  else
    need_dash_server=1
  fi
  if op_field_exists "$DASHBOARD_TUNNEL_ITEM" "client.crt" && op_field_exists "$DASHBOARD_TUNNEL_ITEM" "client.key"; then
    if leaf_expiring_soon "$DASHBOARD_TUNNEL_ITEM" "client.crt"; then
      warn "Dashboard tunnel client certificate expires within ${EXPIRY_THRESHOLD_DAYS} days."
      confirm "Re-issue the dashboard tunnel client cert from the existing CA (resets validity to 825 days)." \
        && need_dash_client=1 || info "Keeping the existing dashboard tunnel client certificate."
    else
      info "Dashboard tunnel client certificate already present; reusing it."
    fi
  else
    need_dash_client=1
  fi
fi

if [[ $need_dash_server -eq 1 ]]; then
  if [[ -z "$DASHBOARD_TUNNEL_IP" ]]; then
    die "Cannot issue the dashboard tunnel server cert: the minikube node IP could not be determined.
Start the cluster (minikube -p ${MINIKUBE_PROFILE} start) or set DASHBOARD_TUNNEL_IP=<node-ip> explicitly,
then re-run. Issuing without the node IP in the SAN would make the host tunnel fail TLS verification."
  fi
  confirm "Issue a dashboard tunnel server cert for CN=$DASHBOARD_TUNNEL_SERVER_CN (SAN: DNS:$DASHBOARD_TUNNEL_SERVER_CN, IP:$DASHBOARD_TUNNEL_IP)." \
    || die "Aborted."
  openssl genrsa -out "$CERTS/dash-server.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$CERTS/dash-server.key" -subj "/CN=${DASHBOARD_TUNNEL_SERVER_CN}" -out "$CERTS/dash-server.csr" >/dev/null 2>&1
  printf 'subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n' "$DASHBOARD_TUNNEL_SERVER_CN" "$DASHBOARD_TUNNEL_IP" > "$CERTS/dash-server.ext"
  openssl x509 -req -in "$CERTS/dash-server.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$CERTS/dash-server.ext" -out "$CERTS/dash-server.crt" >/dev/null 2>&1
  ensure_item "$DASHBOARD_TUNNEL_ITEM"
  set_field "$DASHBOARD_TUNNEL_ITEM" "server.crt" "$CERTS/dash-server.crt" text
  set_field "$DASHBOARD_TUNNEL_ITEM" "server.key" "$CERTS/dash-server.key" concealed
  set_field "$DASHBOARD_TUNNEL_ITEM" "ca.cert"    "$CERTS/ca.crt"          text
  info "Issued + stored dashboard tunnel server cert (pod sidecar, synced into the cluster)."
fi

if [[ $need_dash_client -eq 1 ]]; then
  confirm "Issue a dashboard tunnel client cert for CN=$DASHBOARD_TUNNEL_CLIENT_CN (this is the --allow-cn the pod ghostunnel enforces)." \
    || die "Aborted."
  openssl genrsa -out "$CERTS/dash-client.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$CERTS/dash-client.key" -subj "/CN=${DASHBOARD_TUNNEL_CLIENT_CN}" -out "$CERTS/dash-client.csr" >/dev/null 2>&1
  printf 'extendedKeyUsage=clientAuth\n' > "$CERTS/dash-client.ext"
  openssl x509 -req -in "$CERTS/dash-client.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$CERTS/dash-client.ext" -out "$CERTS/dash-client.crt" >/dev/null 2>&1
  ensure_item "$DASHBOARD_TUNNEL_ITEM"
  set_field "$DASHBOARD_TUNNEL_ITEM" "client.crt" "$CERTS/dash-client.crt" text
  set_field "$DASHBOARD_TUNNEL_ITEM" "client.key" "$CERTS/dash-client.key" concealed
  set_field "$DASHBOARD_TUNNEL_ITEM" "ca.cert"    "$CERTS/ca.crt"          text
  info "Issued + stored dashboard tunnel client cert (laptop-side, pulled to the host)."
fi

# --- Dashboard basic-auth (per-agent) --------------------------------------
# Each agent gets its OWN 1Password item with a random dashboard login password
# and session-signing secret. They are mounted only into that agent's pod (via
# its own OnePasswordItem -> Secret -> secretKeyRef), so no agent can read or
# derive another agent's credentials. Random + per-agent, never in git.
# Add an agent by appending its sandbox name here.
DASHBOARD_AUTH_AGENTS=(${DASHBOARD_AUTH_AGENTS:-hermes})
step "Dashboard basic-auth (per-agent credentials)"
for agent in "${DASHBOARD_AUTH_AGENTS[@]}"; do
  item="Dashboard Auth - ${agent}"
  if op_field_exists "$item" "password" && op_field_exists "$item" "signing-secret"; then
    info "Dashboard auth for '${agent}' already set in '${item}'; reusing."
    continue
  fi
  ensure_item "$item"
  if ! op_field_exists "$item" "password"; then
    openssl rand -base64 24 | tr -d '\n' > "$CERTS/dash-pw"
    set_field "$item" "password" "$CERTS/dash-pw" concealed
  fi
  if ! op_field_exists "$item" "signing-secret"; then
    openssl rand -base64 32 | tr -d '\n' > "$CERTS/dash-sign"
    set_field "$item" "signing-secret" "$CERTS/dash-sign" concealed
  fi
  info "Generated dashboard login for '${agent}' (item '${item}'; synced only into that agent's pod)."
done

# --- Anthropic API key -----------------------------------------------------
step "Anthropic API key"
if op_field_exists "$RUNTIME_ITEM" "Authorization"; then
  info "Anthropic key already set in '$RUNTIME_ITEM' (Authorization); nothing to do."
else
  KEY="${ANTHROPIC_API_KEY:-}"
  if [[ -z "$KEY" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      warn "No ANTHROPIC_API_KEY in environment and --yes is set; leaving Authorization unset."
    else
      echo
      echo "${Y}CHANGE:${N} Store the Anthropic API key in '$RUNTIME_ITEM' (Authorization)."
      read -r -s -p "  Anthropic API key (sk-ant-..., empty to skip): " KEY; echo
    fi
  fi
  if [[ -n "$KEY" ]]; then
    ensure_item "$RUNTIME_ITEM"
    op item edit "$RUNTIME_ITEM" --vault "$VAULT" "Authorization[concealed]=$KEY" >/dev/null
    unset KEY
    info "Stored Anthropic key in '$RUNTIME_ITEM'."
  else
    warn "Authorization left unset — set it later before the agent can route to Anthropic."
  fi
fi

# --- OpenAI API key (optional) ---------------------------------------------
step "OpenAI API key"
if op_field_exists "$OPENAI_ITEM" "Authorization"; then
  info "OpenAI key already set in '$OPENAI_ITEM' (Authorization); nothing to do."
else
  KEY="${OPENAI_API_KEY:-}"
  if [[ -z "$KEY" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      warn "No OPENAI_API_KEY in environment and --yes is set; leaving it unset (optional)."
    else
      echo
      echo "${Y}CHANGE:${N} Store the OpenAI API key in '$OPENAI_ITEM' (Authorization)."
      echo "  Optional — the GPT models simply won't route until it is set."
      read -r -s -p "  OpenAI API key (sk-..., empty to skip): " KEY; echo
    fi
  fi
  ensure_item "$OPENAI_ITEM"
  if [[ -n "$KEY" ]]; then
    op item edit "$OPENAI_ITEM" --vault "$VAULT" "Authorization[concealed]=$KEY" >/dev/null
    unset KEY
    info "Stored OpenAI key in '$OPENAI_ITEM'."
  else
    op item edit "$OPENAI_ITEM" --vault "$VAULT" "Authorization[concealed]=" >/dev/null
    warn "OpenAI Authorization left empty — GPT models are unavailable until set."
  fi
fi

# --- SearXNG secret key ----------------------------------------------------
# Signs SearXNG session/limiter tokens. Generated once and reused so the value
# stays stable across pod restarts (a changed key invalidates SearXNG's cache
# tables and breaks limiter tokens across replicas). Never regenerated unless
# the field is absent.
step "SearXNG secret key"
if op_field_exists "$SEARXNG_ITEM" "secret_key"; then
  info "SearXNG secret key already set in '$SEARXNG_ITEM' (secret_key); nothing to do."
else
  confirm "Generate a SearXNG secret key and store it as item '$SEARXNG_ITEM' (secret_key)." \
    || die "SearXNG secret key is required; aborting."
  SEARXNG_KEY="$(openssl rand -hex 32)"
  ensure_item "$SEARXNG_ITEM"
  op item edit "$SEARXNG_ITEM" --vault "$VAULT" "secret_key[concealed]=$SEARXNG_KEY" >/dev/null
  unset SEARXNG_KEY
  info "Stored SearXNG secret key in '$SEARXNG_ITEM'."
fi

# --- Tavily API key ---------------------------------------------------------
# Read by the Tavily kmcp MCPServer (npx tavily-mcp). The OnePasswordItem syncs
# every field of this item into the Secret as a data key, and kmcp mounts that
# Secret via envFrom, so the field MUST be named exactly TAVILY_API_KEY to land
# as the env var the server reads.
step "Tavily API key"
if op_field_exists "$TAVILY_ITEM" "TAVILY_API_KEY"; then
  info "Tavily key already set in '$TAVILY_ITEM' (TAVILY_API_KEY); nothing to do."
else
  KEY="${TAVILY_API_KEY:-}"
  if [[ -z "$KEY" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      warn "No TAVILY_API_KEY in environment and --yes is set; leaving it unset (web search/extract via Tavily unavailable until set)."
    else
      echo
      echo "${Y}CHANGE:${N} Store the Tavily API key in '$TAVILY_ITEM' (TAVILY_API_KEY)."
      read -r -s -p "  Tavily API key (tvly-..., empty to skip): " KEY; echo
    fi
  fi
  ensure_item "$TAVILY_ITEM"
  if [[ -n "$KEY" ]]; then
    op item edit "$TAVILY_ITEM" --vault "$VAULT" "TAVILY_API_KEY[concealed]=$KEY" >/dev/null
    unset KEY
    info "Stored Tavily key in '$TAVILY_ITEM'."
  else
    warn "Tavily TAVILY_API_KEY left unset — the Tavily MCP server will not authenticate until set."
  fi
fi

# --- Firecrawl API key ------------------------------------------------------
# Read by the Firecrawl kmcp MCPServer (npx firecrawl-mcp). Same envFrom rule as
# Tavily: the field MUST be named exactly FIRECRAWL_API_KEY.
step "Firecrawl API key"
if op_field_exists "$FIRECRAWL_ITEM" "FIRECRAWL_API_KEY"; then
  info "Firecrawl key already set in '$FIRECRAWL_ITEM' (FIRECRAWL_API_KEY); nothing to do."
else
  KEY="${FIRECRAWL_API_KEY:-}"
  if [[ -z "$KEY" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      warn "No FIRECRAWL_API_KEY in environment and --yes is set; leaving it unset (web extract via Firecrawl unavailable until set)."
    else
      echo
      echo "${Y}CHANGE:${N} Store the Firecrawl API key in '$FIRECRAWL_ITEM' (FIRECRAWL_API_KEY)."
      read -r -s -p "  Firecrawl API key (fc-..., empty to skip): " KEY; echo
    fi
  fi
  ensure_item "$FIRECRAWL_ITEM"
  if [[ -n "$KEY" ]]; then
    op item edit "$FIRECRAWL_ITEM" --vault "$VAULT" "FIRECRAWL_API_KEY[concealed]=$KEY" >/dev/null
    unset KEY
    info "Stored Firecrawl key in '$FIRECRAWL_ITEM'."
  else
    warn "Firecrawl FIRECRAWL_API_KEY left unset — the Firecrawl MCP server will not authenticate until set."
  fi
fi

# --- Graphiti FalkorDB password --------------------------------------------
# Auth password for the FalkorDB graph store backing the Graphiti tribal-
# knowledge MCP server. Generated once and reused so the value stays stable
# across pod restarts (both the FalkorDB StatefulSet and the graphiti-mcp pod
# read the same item field). Never regenerated unless the field is absent.
step "Graphiti FalkorDB password"
if op_field_exists "$GRAPHITI_FALKORDB_ITEM" "password"; then
  info "Graphiti FalkorDB password already set in '$GRAPHITI_FALKORDB_ITEM' (password); nothing to do."
else
  confirm "Generate a FalkorDB password and store it as item '$GRAPHITI_FALKORDB_ITEM' (password)." \
    || die "Graphiti FalkorDB password is required; aborting."
  GRAPHITI_FALKORDB_PW="$(openssl rand -hex 24)"
  ensure_item "$GRAPHITI_FALKORDB_ITEM"
  op item edit "$GRAPHITI_FALKORDB_ITEM" --vault "$VAULT" "password[concealed]=$GRAPHITI_FALKORDB_PW" >/dev/null
  unset GRAPHITI_FALKORDB_PW
  info "Stored Graphiti FalkorDB password in '$GRAPHITI_FALKORDB_ITEM'."
fi

# --- Graphiti gateway virtual key ------------------------------------------
# The virtual API key the Graphiti MCP server presents to agentgateway for
# embeddings. It only needs to be a non-empty string (the OpenAI SDK rejects an
# empty key at client construction); the real OpenAI key lives in the gateway's
# OpenAI item and is swapped in by the embeddings backend. Generated once and
# reused. Never regenerated unless the field is absent.
step "Graphiti gateway virtual key"
if op_field_exists "$GRAPHITI_GATEWAY_KEY_ITEM" "api-key"; then
  info "Graphiti gateway key already set in '$GRAPHITI_GATEWAY_KEY_ITEM' (api-key); nothing to do."
else
  confirm "Generate a Graphiti gateway virtual key and store it as item '$GRAPHITI_GATEWAY_KEY_ITEM' (api-key)." \
    || die "Graphiti gateway virtual key is required; aborting."
  GRAPHITI_GATEWAY_KEY="$(openssl rand -hex 32)"
  ensure_item "$GRAPHITI_GATEWAY_KEY_ITEM"
  op item edit "$GRAPHITI_GATEWAY_KEY_ITEM" --vault "$VAULT" "api-key[concealed]=$GRAPHITI_GATEWAY_KEY" >/dev/null
  unset GRAPHITI_GATEWAY_KEY
  info "Stored Graphiti gateway virtual key in '$GRAPHITI_GATEWAY_KEY_ITEM'."
fi

# --- verify ----------------------------------------------------------------
step "Verify"
missing=0
check() {
  if op_field_exists "$1" "$2"; then
    echo "  ${G}ok${N}   op://$VAULT/$1/$2"
  else
    echo "  ${R}MISS${N} op://$VAULT/$1/$2"
    missing=1
  fi
}
check "$CRED_ITEM" "1password-credentials.json"
check "$TOKEN_ITEM" "token"
check "$CA_ITEM" "ca.cert"
check "$RUNTIME_ITEM" "tls.crt"
check "$RUNTIME_ITEM" "tls.key"
check "$RUNTIME_ITEM" "Authorization"
check "$HOST_ITEM" "server.crt"
check "$HOST_ITEM" "server.key"
check "$HOST_ITEM" "ca.cert"
check "$HOST_ITEM" "ca.key"
check "$DASHBOARD_TUNNEL_ITEM" "server.crt"
check "$DASHBOARD_TUNNEL_ITEM" "server.key"
check "$DASHBOARD_TUNNEL_ITEM" "client.crt"
check "$DASHBOARD_TUNNEL_ITEM" "client.key"
check "$DASHBOARD_TUNNEL_ITEM" "ca.cert"
check "$SEARXNG_ITEM" "secret_key"
check "$GRAPHITI_FALKORDB_ITEM" "password"
check "$GRAPHITI_GATEWAY_KEY_ITEM" "api-key"

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material is present in 1Password."
  echo "Next: run scripts/install/install.sh to bootstrap Connect + Flux, and"
  echo "      scripts/ghostunnel/ghostshell.sh on the host to start the mTLS tunnel."
else
  warn "Some fields are still missing (see above). Re-run to complete them."
  exit 1
fi
