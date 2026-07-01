#!/usr/bin/env bash
# Idempotent 1Password setup for a single vicegerent agent.
#
# Usage: setup-secrets-agent.sh <agent-name> [-y|--yes]
#
# Provisions, in 1Password vault "Vicegerent", the per-agent items for <agent-name>:
#   - Agent - <name>                    password, signing-secret  (dashboard auth)
#                                       SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
#                                       SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (optional)
#                                       public-key (text)         (→ agent-sandbox only)
#   - Agent - <name> SSH Key            ed25519 keypair (1Password Document)
#   - Agent - <name> agentgateway API key   api-key (random hex bearer token)
#
# Each agent gets its own independently generated bearer token, dashboard
# credentials, and SSH key — no material is shared between agents.
#
# Properties:
#   - Idempotent: anything already present in 1Password is reused, never regenerated.
#   - Self-cleaning: all key material is written only inside a private tmpdir that is
#     removed on any exit (including Ctrl-C). Nothing is left on disk.
#   - Verbose: every mutating step is announced and confirmed before it runs.
#
# Flags:
#   -y, --yes     auto-approve every change (non-interactive)
#   -h, --help    show this help

set -euo pipefail

VAULT="${VAULT:-Vicegerent}"

ASSUME_YES=0
AGENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [[ -z "$AGENT" ]] && AGENT="$1" || { echo "unexpected argument: $1" >&2; exit 2; } ;;
  esac
  shift
done

[[ -n "$AGENT" ]] || { echo "usage: $0 <agent-name> [-y|--yes]" >&2; exit 2; }

ITEM="Agent - $AGENT"
ITEM_SSH="Agent - $AGENT SSH Key"
ITEM_API_KEY="Agent - $AGENT agentgateway API key"  # pragma: allowlist secret

# Fixed key filename: the OnePasswordItem syncs the document into a Secret keyed by
# this name, and every agent's sandbox mounts it at /opt/hermes-ssh/<name>, so it
# must match GIT_SSH_COMMAND in the chart (hermes_agent_ed25519) for all agents.
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
  local json_type="${type^^}"
  op item get "$title" --vault "$VAULT" --format json \
    | jq --arg label "$field" --arg type "$json_type" --rawfile value "$file" \
        '.fields |= (map(select(.label != $label))
          + [{"id": $label, "label": $label, "type": $type, "value": $value}])' \
    | op item edit "$title" --vault "$VAULT" >/dev/null
}

# --- prerequisites ---------------------------------------------------------
for cmd in op openssl ssh-keygen jq; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH"
done
op account get >/dev/null 2>&1 || die "1Password CLI is not signed in. Run: op signin"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-agent-setup.XXXXXX")"
chmod 700 "$WORK"
CERTS="$WORK/certs"
mkdir -p "$CERTS"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

echo "${B}vicegerent agent secret setup${N}  (agent: $AGENT, vault: $VAULT)"
echo "Scratch dir: $WORK  — removed automatically on exit."

# --- dashboard auth --------------------------------------------------------
# password + signing-secret (random, generated once, never regenerated).
step "$ITEM dashboard auth"
ensure_item "$ITEM"
any_dashboard_missing=0
op_field_exists "$ITEM" "password"        || any_dashboard_missing=1
op_field_exists "$ITEM" "signing-secret"  || any_dashboard_missing=1
if [[ $any_dashboard_missing -eq 0 ]]; then
  info "Dashboard auth already set in '$ITEM'; reusing."
else
  confirm "Generate missing dashboard auth credentials in '$ITEM'." \
    || die "Dashboard auth is required; aborting."
  if ! op_field_exists "$ITEM" "password"; then
    openssl rand -base64 24 | tr -d '\n' > "$CERTS/dash-pw"
    set_field "$ITEM" "password" "$CERTS/dash-pw" concealed
    info "Generated dashboard password in '$ITEM'."
  fi
  if ! op_field_exists "$ITEM" "signing-secret"; then
    openssl rand -base64 32 | tr -d '\n' > "$CERTS/dash-sign"
    set_field "$ITEM" "signing-secret" "$CERTS/dash-sign" concealed
    info "Generated dashboard signing-secret in '$ITEM'."
  fi
fi

# --- Slack bot credentials (optional) -------------------------------------
# SLACK_BOT_TOKEN   — xoxb-... Bot User OAuth Token (installed to workspace)
# SLACK_APP_TOKEN   — xapp-... App-Level Token (Socket Mode, connections:write scope)
# SLACK_ALLOWED_USERS — space-separated Slack user IDs allowed to talk to the bot
# SLACK_HOME_CHANNEL  — Slack channel ID for proactive messages (cron output etc.)
#
# Stored in '$ITEM' with field names matching the env var names. Slack activates
# once these fields are set. Create the Slack app with:
#   vicegerent slack manifest "<BotName>" | pbcopy
# Paste the manifest at api.slack.com → Your Apps → Create New App → From manifest,
# then install it to your workspace. Socket Mode must be enabled (Settings → Socket Mode).
step "$ITEM Slack credentials (optional — skip to configure later)"
slack_any_missing=0
op_field_exists "$ITEM" "SLACK_BOT_TOKEN"     || slack_any_missing=1
op_field_exists "$ITEM" "SLACK_APP_TOKEN"     || slack_any_missing=1
op_field_exists "$ITEM" "SLACK_ALLOWED_USERS" || slack_any_missing=1
op_field_exists "$ITEM" "SLACK_HOME_CHANNEL"  || slack_any_missing=1

if [[ $slack_any_missing -eq 0 ]]; then
  info "Slack credentials already set in '$ITEM'; nothing to do."
else
  echo
  echo "  Create your Slack app with the generated manifest:"
  echo "    vicegerent slack manifest \"<BotName>\" | pbcopy"
  echo "  then go to api.slack.com → Your Apps → Create New App → From manifest."
  echo "  Enable Socket Mode (Settings → Socket Mode) and install to your workspace."
  echo "  The agent pod starts without these — Slack activates once the item is populated."
  echo

  if ! op_field_exists "$ITEM" "SLACK_BOT_TOKEN"; then
    KEY="${SLACK_BOT_TOKEN:-}"
    if [[ -z "$KEY" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_BOT_TOKEN in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack Bot User OAuth Token in '$ITEM' (SLACK_BOT_TOKEN)."
        read -r -s -p "  Bot token (xoxb-..., empty to skip): " KEY; echo
      fi
    fi
    if [[ -n "$KEY" ]]; then
      printf '%s' "$KEY" > "$CERTS/slack-bot-token"
      set_field "$ITEM" "SLACK_BOT_TOKEN" "$CERTS/slack-bot-token" concealed
      unset KEY
      info "Stored SLACK_BOT_TOKEN in '$ITEM'."
    else
      warn "SLACK_BOT_TOKEN left unset — Slack gateway inactive until set."
    fi
  else
    info "SLACK_BOT_TOKEN already set in '$ITEM'; reusing."
  fi

  if ! op_field_exists "$ITEM" "SLACK_APP_TOKEN"; then
    KEY="${SLACK_APP_TOKEN:-}"
    if [[ -z "$KEY" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_APP_TOKEN in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack App-Level Token in '$ITEM' (SLACK_APP_TOKEN)."
        echo "  (Settings → Basic Information → App-Level Tokens → connections:write scope)"
        read -r -s -p "  App token (xapp-..., empty to skip): " KEY; echo
      fi
    fi
    if [[ -n "$KEY" ]]; then
      printf '%s' "$KEY" > "$CERTS/slack-app-token"
      set_field "$ITEM" "SLACK_APP_TOKEN" "$CERTS/slack-app-token" concealed
      unset KEY
      info "Stored SLACK_APP_TOKEN in '$ITEM'."
    else
      warn "SLACK_APP_TOKEN left unset — Socket Mode inactive until set."
    fi
  else
    info "SLACK_APP_TOKEN already set in '$ITEM'; reusing."
  fi

  if ! op_field_exists "$ITEM" "SLACK_ALLOWED_USERS"; then
    VAL="${SLACK_ALLOWED_USERS:-}"
    if [[ -z "$VAL" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_ALLOWED_USERS in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store allowed Slack user IDs in '$ITEM' (SLACK_ALLOWED_USERS)."
        echo "  Space-separated Slack user IDs (e.g. U04B7TU3HL7). Find yours via api.slack.com/methods/auth.test."
        read -r -p "  Allowed user IDs (empty to skip): " VAL; echo
      fi
    fi
    if [[ -n "$VAL" ]]; then
      op item edit "$ITEM" --vault "$VAULT" "SLACK_ALLOWED_USERS[text]=$VAL" >/dev/null
      unset VAL
      info "Stored SLACK_ALLOWED_USERS in '$ITEM'."
    else
      warn "SLACK_ALLOWED_USERS left unset — bot will reject all messages until set."
    fi
  else
    info "SLACK_ALLOWED_USERS already set in '$ITEM'; reusing."
  fi

  if ! op_field_exists "$ITEM" "SLACK_HOME_CHANNEL"; then
    VAL="${SLACK_HOME_CHANNEL:-}"
    if [[ -z "$VAL" ]]; then
      if [[ "$ASSUME_YES" == "1" ]]; then
        warn "No SLACK_HOME_CHANNEL in environment and --yes is set; leaving unset (optional)."
      else
        echo "${Y}CHANGE:${N} Store the Slack home channel ID in '$ITEM' (SLACK_HOME_CHANNEL)."
        echo "  Channel ID where the bot delivers cron output and proactive messages (e.g. C04B7TU3HL7)."
        echo "  Right-click a channel in Slack → View channel details → copy the ID at the bottom."
        read -r -p "  Home channel ID (empty to skip): " VAL; echo
      fi
    fi
    if [[ -n "$VAL" ]]; then
      op item edit "$ITEM" --vault "$VAULT" "SLACK_HOME_CHANNEL[text]=$VAL" >/dev/null
      unset VAL
      info "Stored SLACK_HOME_CHANNEL in '$ITEM'."
    else
      warn "SLACK_HOME_CHANNEL left unset — cron/proactive output has no home channel until set."
    fi
  else
    info "SLACK_HOME_CHANNEL already set in '$ITEM'; reusing."
  fi
fi

# --- SSH key ---------------------------------------------------------------
# Dedicated ed25519 key (generate-once, stored as a 1Password Document so newlines
# are preserved exactly). The public key is stored as a text field in '$ITEM'.
step "$ITEM SSH key"
if op document get "$ITEM_SSH" --vault "$VAULT" &>/dev/null; then
  info "SSH key document '$ITEM_SSH' already exists; nothing to do."
  echo
  echo "  ${Y}Public key${N} (add to GitLab/GitHub if you haven't already):"
  op item get "$ITEM" --vault "$VAULT" --fields label=public-key --reveal 2>/dev/null | tr -d '"' || true
else
  if confirm "Generate a new ed25519 SSH key for agent '$AGENT' and store it as '$ITEM_SSH'."; then
    ssh-keygen -t ed25519 -C "${AGENT}-agent@vicegerent" -N "" -f "$CERTS/$SSH_KEY_FILE" >/dev/null 2>&1
    op document create "$CERTS/$SSH_KEY_FILE" \
      --title "$ITEM_SSH" \
      --vault "$VAULT"
    set_field "$ITEM" "public-key" "$CERTS/$SSH_KEY_FILE.pub" text
    info "Stored SSH key document in '$ITEM_SSH'."
    echo
    echo "  ${Y}Next step:${N} Add the public key to your git hosts (GitLab/GitHub deploy keys):"
    cat "$CERTS/$SSH_KEY_FILE.pub"
  else
    warn "SSH key generation skipped — git push/pull from the sandbox will not work until set."
  fi
fi

# --- agentgateway virtual API key ------------------------------------------
# Random bearer token the agent presents to agentgateway (generate-once, per agent).
step "$ITEM_API_KEY"
ensure_item "$ITEM_API_KEY"
if op_field_exists "$ITEM_API_KEY" "api-key"; then  # pragma: allowlist secret
  info "agentgateway API key already in '$ITEM_API_KEY'; reusing."
else
  printf '%s' "$(openssl rand -hex 32)" > "$CERTS/agentgateway-api-key"
  set_field "$ITEM_API_KEY" "api-key" "$CERTS/agentgateway-api-key" concealed  # pragma: allowlist secret
  info "Generated agentgateway API key and stored it in '$ITEM_API_KEY'."
fi

# --- verify ----------------------------------------------------------------
step "Verify"
missing=0
check() {
  if op_field_exists "$1" "$2"; then
    echo "  ${G}ok${N}   $1/$2"
  else
    echo "  ${R}MISS${N} $1/$2"
    missing=1
  fi
}
check_optional() {
  if op_field_exists "$1" "$2"; then
    echo "  ${G}ok${N}   $1/$2"
  else
    echo "  ${Y}skip${N} $1/$2  (optional — set later)"
  fi
}
check "$ITEM" "password"
check "$ITEM" "signing-secret"
check "$ITEM_API_KEY" "api-key"
check_optional "$ITEM" "SLACK_BOT_TOKEN"
check_optional "$ITEM" "SLACK_APP_TOKEN"
check_optional "$ITEM" "SLACK_ALLOWED_USERS"
check_optional "$ITEM" "SLACK_HOME_CHANNEL"
check_optional "$ITEM" "public-key"
if op document get "$ITEM_SSH" --vault "$VAULT" &>/dev/null; then
  echo "  ${G}ok${N}   $ITEM_SSH (ssh key document)"
else
  echo "  ${Y}skip${N} $ITEM_SSH (ssh key document — run setup to generate)"
fi

echo
if [[ $missing -eq 0 ]]; then
  info "All required secret material for agent '$AGENT' is present in 1Password."
else
  warn "Some fields are still missing (see above). Re-run to complete them."
  exit 1
fi
