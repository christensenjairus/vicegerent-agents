#!/usr/bin/env bash

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

KUBE_CONTEXT="${KUBE_CONTEXT:-vicegerent}"
REPO_URL="${REPO_URL:-ssh://git@gitlab.hahomelabs.com/jchristensen/vicegerent-agents.git}"
BRANCH="${BRANCH:-main}"
CLUSTER_PATH="${CLUSTER_PATH:-./clusters/vicegerent}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-$HOME/.ssh/id_rsa}"

for cmd in kubectl flux; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo -e "${RED}ERROR:${NC} $cmd is not installed or not on PATH" >&2
    exit 1
  fi
done

if ! kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1; then
  echo -e "${RED}ERROR:${NC} kubectl context '$KUBE_CONTEXT' does not exist" >&2
  exit 1
fi

if [[ ! -f "$PRIVATE_KEY_FILE" ]]; then
  echo -e "${RED}ERROR:${NC} private key not found: $PRIVATE_KEY_FILE" >&2
  exit 1
fi

echo "Target context: $KUBE_CONTEXT"
echo "Repository:     $REPO_URL"
echo "Branch:         $BRANCH"
echo "Cluster path:   $CLUSTER_PATH"
echo "Private key:    $PRIVATE_KEY_FILE"
echo
kubectl --context "$KUBE_CONTEXT" cluster-info >/dev/null
kubectl --context "$KUBE_CONTEXT" get nodes -o wide

echo
read -r -p "Bootstrap Flux onto context '$KUBE_CONTEXT'? (y/N) " reply
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  echo "Exiting without changes."
  exit 1
fi

flux bootstrap git \
  --url="$REPO_URL" \
  --branch="$BRANCH" \
  --path="$CLUSTER_PATH" \
  --private-key-file="$PRIVATE_KEY_FILE" \
  --context="$KUBE_CONTEXT"

echo -e "${GREEN}Flux bootstrap complete.${NC}"
echo "Check reconciliation with:"
echo "  flux --context $KUBE_CONTEXT get all -A"
