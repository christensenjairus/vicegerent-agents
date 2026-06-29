#!/usr/bin/env bash
# Idempotent 1Password + ghostunnel setup for the vicegerent platform.
#
# Provisions, in 1Password vault "Vicegerent":
#   - the vault itself
#   - a Connect server + its credentials file (item "Connect Credentials")
#   - a Connect operator token (item "Connect Token")
#   - ghostunnel mTLS certificates split across four items:
#       MCP Client     tls.crt, tls.key              (synced to agentgateway-system only)
#       MCP CA         ca.cert                        (synced to agentgateway-system only)
#       Ghostunnel Host  server.crt, server.key, ca.cert, ca.key  (host-only, never synced)
#       Runtime        Authorization                  (synced to agentgateway-system only)
#   - a SearXNG secret key (item "SearXNG") synced into the cluster
#   - an SSH private key (item "Hermes SSH Key") for git push/pull from inside the sandbox
#     (auto-generated ed25519 key; never uses one of the user's existing keys)

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
CLIENT_ITEM="MCP Client"
CA_ITEM="MCP CA"
HOST_ITEM="Ghostunnel Host"
CRED_ITEM="Connect Credentials"
OPENAI_ITEM="OpenAI"
SEARXNG_ITEM="SearXNG"
TAVILY_ITEM="Tavily"
FIRECRAWL_ITEM="Firecrawl"
SLACK_ITEM="Hermes Bot Secrets"
SSH_KEY_ITEM="Hermes SSH Key"
TOKEN_ITEM="Connect Token"

HOST_ONLY_IP="${HOST_ONLY_IP:-192.168.64.1}"
SERVER_CN="${SERVER_CN:-host.minikube.internal}"
CLIENT_CN="${CLIENT_CN:-agent-client}"

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

ensure_item() {
  local title="$1"
  if ! op_item_exists "$title"; then
    op item create --category "Secure Note" --vault "$VAULT" --title "$title" >/dev/null
  fi
}

# set_field <item> <field> <file> [concealed|text]
set_field() {
  local title="$1" field="$2" file="$3" type="${4:-concealed}"
  # Use op item get + jq --rawfile to safely encode multi-line file content
  # (e.g. PEM private keys). Shell argument interpolation with $(cat) strips
  # trailing newlines and op may misparse embedded newlines in argument values.
  # jq --rawfile reads bytes verbatim and JSON-encodes them; op item edit reads
  # the full item JSON from stdin and applies the update cleanly.
  local json_type="${type^^}"
  op item get "$title" --vault "$VAULT" --format json \
    | jq --arg label "$field" --arg type "$json_type" --rawfile value "$file" \
        '.fields |= (map(select(.label != $label))
          + [{"id": $label, "label": $label, "type": $type, "value": $value}])' \
    | op item edit "$title" --vault "$VAULT" >/dev/null
}

set_value_field() {
  local title="$1" field="$2" value="$3" type="${4:-concealed}"
  local tmp
  tmp="$(mktemp "$CERTS/field.XXXXXX")"
  printf %s "$value" > "$tmp"
  set_field "$title" "$field" "$tmp" "$type"
}

connect_server_exists() {
  op connect server list --format json 2>/dev/null \
    | jq -e --arg n "$SERVER_NAME" '.[]? | select(.name==$n)' >/dev/null 2>&1
}

# --- prerequisites ---------------------------------------------------------
for cmd in op openssl ssh-keygen jq; do
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
op_field_exists "$CLIENT_ITEM" "tls.crt" && leaf_present=1
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
  if op_field_exists "$CLIENT_ITEM" "tls.crt" && op_field_exists "$CLIENT_ITEM" "tls.key"; then
    if leaf_expiring_soon "$CLIENT_ITEM" "tls.crt"; then
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
  ensure_item "$CLIENT_ITEM"
  set_field "$CLIENT_ITEM" "tls.crt" "$CERTS/client.crt" text
  set_field "$CLIENT_ITEM" "tls.key" "$CERTS/client.key" concealed
  info "Set '$CLIENT_ITEM' tls.crt + tls.key (client identity, synced to agentgateway-system only)."
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

# --- Slack bot credentials (optional) -------------------------------------
# SLACK_BOT_TOKEN   — xoxb-... Bot User OAuth Token (installed to workspace)
# SLACK_APP_TOKEN   — xapp-... App-Level Token (Socket Mode, connections:write scope)
# SLACK_ALLOWED_USERS — space-separated Slack user IDs allowed to talk to the bot
# SLACK_HOME_CHANNEL  — Slack channel ID for proactive messages (cron output etc.)
#
# Stored in 1Password item "Hermes Bot Secrets" with field names matching the
# env var names. The secret is mounted via envFrom into the hermes pod with
# optional: true, so the pod starts without it and Slack activates once the
# item is populated and Connect syncs the secret.
#
# Create the Slack app with:
#   hermes slack manifest --name "<BotName>" | pbcopy
# Paste the manifest at api.slack.com → Your Apps → Create New App → From manifest,
# then install it to your workspace. Socket Mode must be enabled (Settings → Socket Mode).
step "Slack bot credentials (optional — skip to configure later)"
ensure_item "$SLACK_ITEM"
slack_any_missing=0
op_field_exists "$SLACK_ITEM" "SLACK_BOT_TOKEN"    || slack_any_missing=1
op_field_exists "$SLACK_ITEM" "SLACK_APP_TOKEN"    || slack_any_missing=1
op_field_exists "$SLACK_ITEM" "SLACK_ALLOWED_USERS" || slack_any_missing=1
op_field_exists "$SLACK_ITEM" "SLACK_HOME_CHANNEL"  || slack_any_missing=1

if [[ $slack_any_missing -eq 0 ]]; then
  info "Slack credentials already set in '$SLACK_ITEM'; nothing to do."
else
  echo
  echo "  Create your Slack app with the generated manifest:"
  echo "    hermes slack manifest --name \"<BotName>\" | pbcopy"
  echo "  then go to api.slack.com → Your Apps → Create New App → From manifest."
  echo "  Enable Socket Mode (Settings → Socket Mode) and install to your workspace."
  echo "  The hermes pod starts without these — Slack activates once the item is populated."
  echo

  if ! op_field_exists "$SLACK_ITEM" "SLACK_BOT_TOKEN"; then
    KEY="${SLACK_BOT_TOKEN:-}"
    if [[ -z "$KEY" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_BOT_TOKEN in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack Bot User OAuth Token in '$SLACK_ITEM' (SLACK_BOT_TOKEN)."
        read -r -s -p "  Bot token (xoxb-..., empty to skip): " KEY; echo
      fi
    fi
    if [[ -n "$KEY" ]]; then
      op item edit "$SLACK_ITEM" --vault "$VAULT" "SLACK_BOT_TOKEN[concealed]=$KEY" >/dev/null
      unset KEY
      info "Stored SLACK_BOT_TOKEN in '$SLACK_ITEM'."
    else
      warn "SLACK_BOT_TOKEN left unset — Slack gateway inactive until set."
    fi
  else
    info "SLACK_BOT_TOKEN already set in '$SLACK_ITEM'; reusing."
  fi

  if ! op_field_exists "$SLACK_ITEM" "SLACK_APP_TOKEN"; then
    KEY="${SLACK_APP_TOKEN:-}"
    if [[ -z "$KEY" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_APP_TOKEN in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack App-Level Token in '$SLACK_ITEM' (SLACK_APP_TOKEN)."
        echo "  (Settings → Basic Information → App-Level Tokens → connections:write scope)"
        read -r -s -p "  App token (xapp-..., empty to skip): " KEY; echo
      fi
    fi
    if [[ -n "$KEY" ]]; then
      op item edit "$SLACK_ITEM" --vault "$VAULT" "SLACK_APP_TOKEN[concealed]=$KEY" >/dev/null
      unset KEY
      info "Stored SLACK_APP_TOKEN in '$SLACK_ITEM'."
    else
      warn "SLACK_APP_TOKEN left unset — Socket Mode inactive until set."
    fi
  else
    info "SLACK_APP_TOKEN already set in '$SLACK_ITEM'; reusing."
  fi

  if ! op_field_exists "$SLACK_ITEM" "SLACK_ALLOWED_USERS"; then
    VAL="${SLACK_ALLOWED_USERS:-}"
    if [[ -z "$VAL" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_ALLOWED_USERS in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store allowed Slack user IDs in '$SLACK_ITEM' (SLACK_ALLOWED_USERS)."
        echo "  Space-separated Slack user IDs (e.g. U04B7TU3HL7). Find yours via api.slack.com/methods/auth.test."
        read -r -p "  Allowed user IDs (empty to skip): " VAL; echo
      fi
    fi
    if [[ -n "$VAL" ]]; then
      op item edit "$SLACK_ITEM" --vault "$VAULT" "SLACK_ALLOWED_USERS[text]=$VAL" >/dev/null
      unset VAL
      info "Stored SLACK_ALLOWED_USERS in '$SLACK_ITEM'."
    else
      warn "SLACK_ALLOWED_USERS left unset — bot will reject all messages until set."
    fi
  else
    info "SLACK_ALLOWED_USERS already set in '$SLACK_ITEM'; reusing."
  fi

  if ! op_field_exists "$SLACK_ITEM" "SLACK_HOME_CHANNEL"; then
    VAL="${SLACK_HOME_CHANNEL:-}"
    if [[ -z "$VAL" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_HOME_CHANNEL in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack home channel ID in '$SLACK_ITEM' (SLACK_HOME_CHANNEL)."
        echo "  Channel ID where the bot delivers cron output and proactive messages (e.g. C04B7TU3HL7)."
        echo "  Right-click a channel in Slack → View channel details → copy the ID at the bottom."
        read -r -p "  Home channel ID (empty to skip): " VAL; echo
      fi
    fi
    if [[ -n "$VAL" ]]; then
      op item edit "$SLACK_ITEM" --vault "$VAULT" "SLACK_HOME_CHANNEL[text]=$VAL" >/dev/null
      unset VAL
      info "Stored SLACK_HOME_CHANNEL in '$SLACK_ITEM'."
    else
      warn "SLACK_HOME_CHANNEL left unset — cron/proactive output has no home channel until set."
    fi
  else
    info "SLACK_HOME_CHANNEL already set in '$SLACK_ITEM'; reusing."
  fi
fi

# --- SSH key for git push/pull ------------------------------------------------
# Generates a dedicated ed25519 key for the hermes agent (generate-once, stored
# in 1Password). The private key is concealed; the public key is stored as a
# text field so it is easy to find and add to GitLab/GitHub deploy keys.
# Never asks the user to upload one of their existing keys.
step "SSH key for git push/pull"
ensure_item "$SSH_KEY_ITEM"
if op_field_exists "$SSH_KEY_ITEM" "private-key"; then
  info "SSH private key already in '$SSH_KEY_ITEM' (private-key); nothing to do."
  echo
  echo "  ${Y}Public key${N} (add to GitLab/GitHub if you haven't already):"
  op read "op://$VAULT/$SSH_KEY_ITEM/public-key" 2>/dev/null || true
else
  confirm "Generate a new ed25519 SSH key for the hermes agent and store it in '$SSH_KEY_ITEM'." \
    || { warn "SSH key generation skipped — git push/pull from the sandbox will not work until set."; }
  if [[ $? -eq 0 ]]; then
    ssh-keygen -t ed25519 -C "hermes-agent@vicegerent" -N "" -f "$CERTS/hermes_agent_ed25519" >/dev/null 2>&1
    set_field "$SSH_KEY_ITEM" "private-key" "$CERTS/hermes_agent_ed25519" concealed
    set_field "$SSH_KEY_ITEM" "public-key"  "$CERTS/hermes_agent_ed25519.pub" text
    info "Stored generated SSH key in '$SSH_KEY_ITEM'."
    echo
    echo "  ${Y}Next step:${N} Add the public key to your git hosts (GitLab/GitHub deploy keys):"
    cat "$CERTS/hermes_agent_ed25519.pub"
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
check "$CLIENT_ITEM" "tls.crt"
check "$CLIENT_ITEM" "tls.key"
check "$RUNTIME_ITEM" "Authorization"
check "$HOST_ITEM" "server.crt"
check "$HOST_ITEM" "server.key"
check "$HOST_ITEM" "ca.cert"
check "$HOST_ITEM" "ca.key"
check "$SEARXNG_ITEM" "secret_key"
# Slack is optional — warn but don't fail if absent (pod starts without it).
check_optional() {
  if op_field_exists "$1" "$2"; then
    echo "  ${G}ok${N}   op://$VAULT/$1/$2"
  else
    echo "  ${Y}skip${N} op://$VAULT/$1/$2  (optional — set later to enable Slack)"
  fi
}
check_optional "$SLACK_ITEM" "SLACK_BOT_TOKEN"
check_optional "$SLACK_ITEM" "SLACK_APP_TOKEN"
check_optional "$SLACK_ITEM" "SLACK_ALLOWED_USERS"
check_optional "$SLACK_ITEM" "SLACK_HOME_CHANNEL"
check_optional "$SSH_KEY_ITEM" "private-key"

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material is present in 1Password."
else
  warn "Some fields are still missing (see above). Re-run to complete them."
  exit 1
fi
