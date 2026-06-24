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
CRED_ITEM="Connect Credentials"
TOKEN_ITEM="Connect Token"

HOST_ONLY_IP="${HOST_ONLY_IP:-192.168.64.1}"
SERVER_CN="${SERVER_CN:-host.minikube.internal}"
CLIENT_CN="${CLIENT_CN:-agent-client}"

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
if op_item_exists "$CRED_ITEM" && op_field_exists "$CRED_ITEM" "1password-credentials.json"; then
  info "Connect credentials already in 1Password ('$CRED_ITEM'); not touching the Connect server."
else
  if connect_server_exists; then
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
    info "Server certificate already present; reusing it."
  else
    need_server=1
  fi
  if op_field_exists "$RUNTIME_ITEM" "tls.crt" && op_field_exists "$RUNTIME_ITEM" "tls.key"; then
    info "Client certificate already present; reusing it."
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

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material is present in 1Password."
  echo "Next: run tools/install/install.sh to bootstrap Connect + Flux, and"
  echo "      tools/ghostunnel/ghostshell.sh on the host to start the mTLS tunnel."
else
  warn "Some fields are still missing (see above). Re-run to complete them."
  exit 1
fi
