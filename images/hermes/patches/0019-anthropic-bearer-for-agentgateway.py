#!/usr/bin/env python3
"""Vicegerent patch: send Bearer auth (not x-api-key) to our in-cluster
agentgateway /anthropic route, so agentgateway's apiKeyAuthentication policy
(HAH-109) can validate Hermes's own requests the same way it already
validates Codex's and Claude Code's.

Context
-------
agent/anthropic_adapter.py's _requires_bearer_auth() decides whether the
Anthropic SDK client is built with auth_token= (sends "Authorization: Bearer
***") or api_key= (sends "X-Api-Key: ***"). It only returns True for a
hardcoded set of third-party hosts that are KNOWN to require Bearer auth
(MiniMax's global/China Anthropic-compatible endpoints, Azure AI Foundry).

This platform's model.base_url for the anthropic provider is
"http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic"
(see charts/agent/templates/_helpers.tpl's key_env: ANTHROPIC_API_KEY entry).
That hostname is not on the Bearer allowlist, so Hermes's own outbound
Anthropic calls go through api_key= and send "X-Api-Key: <token>" --
never "Authorization: Bearer <token>".

HAH-109 (apps/base/gateway/apikey-policy.yaml) puts an agentgateway
apiKeyAuthentication policy on the /anthropic, /openai, and /haiku-oai
routes. Per agentgateway's own APIKeyAuthentication.location field (default
unset -> Authorization header with Bearer prefix), the policy validates
Authorization: Bearer, not X-Api-Key. Codex (experimental_bearer_token) and
Claude Code (ANTHROPIC_AUTH_TOKEN) both already send Bearer and pass this
gate; Hermes's own model calls -- and any auxiliary/subagent call built
through this same adapter -- would 401 the moment that policy goes live,
because they carry the wrong header for a credential the policy never sees.

Reproduced: traced _requires_bearer_auth()'s allowlist directly and
confirmed agentgateway-proxy.agentgateway-system.svc.cluster.local matches
neither the MiniMax hostname prefixes nor "azure.com" -- confirmed by
inspection of the live installed module, not assumption.

Fix
---
Add a check for agentgateway-proxy's in-cluster hostname (matched on the
".agentgateway-system.svc.cluster.local" suffix, not a bare "/anthropic"
path substring -- official api.anthropic.com and other proxies that
legitimately use /anthropic as a path prefix must keep using x-api-key,
so this must not become a generic /anthropic-path rule). Purely additive:
extends the boolean OR, does not touch the MiniMax/Azure branches or any
other resolution logic.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this function), the patch raises and the image build
fails, signalling a re-verify.

Remove once this repo's own agentgateway route stops requiring
apiKeyAuthentication, or once Hermes gains a config-level per-provider
auth-scheme override that doesn't require patching source.
"""
import importlib.util
import sys

ANCHOR = (
    "    normalized = _normalize_base_url_text(base_url)\n"
    "    if not normalized:\n"
    "        return False\n"
    "    normalized = normalized.rstrip(\"/\").lower()\n"
    "    return (\n"
    "        normalized.startswith((\"https://api.minimax.io/anthropic\", \"https://api.minimaxi.com/anthropic\"))\n"
    "        or \"azure.com\" in normalized\n"
    "    )\n"
)

REPLACEMENT = (
    "    normalized = _normalize_base_url_text(base_url)\n"
    "    if not normalized:\n"
    "        return False\n"
    "    normalized = normalized.rstrip(\"/\").lower()\n"
    "    return (\n"
    "        normalized.startswith((\"https://api.minimax.io/anthropic\", \"https://api.minimaxi.com/anthropic\"))\n"
    "        or \"azure.com\" in normalized\n"
    "        # Vicegerent: our in-cluster agentgateway /anthropic route gates on\n"
    "        # apiKeyAuthentication, which validates Authorization: Bearer, not\n"
    "        # x-api-key (see HAH-109, apps/base/gateway/apikey-policy.yaml).\n"
    "        # Matched on the in-cluster Service FQDN suffix, not a bare\n"
    "        # \"/anthropic\" path substring, so official api.anthropic.com and\n"
    "        # other third-party /anthropic proxies keep using x-api-key.\n"
    "        or \".agentgateway-system.svc.cluster.local\" in normalized\n"
    "    )\n"
)

MODULE_NAME = "agent.anthropic_adapter"
spec = importlib.util.find_spec(MODULE_NAME)
if spec is None or spec.origin is None:
    print(f"patch: could not locate module {MODULE_NAME!r}", file=sys.stderr)
    sys.exit(1)

path = spec.origin
with open(path, "r", encoding="utf-8") as fh:
    source = fh.read()

count = source.count(ANCHOR)
if count != 1:
    print(
        f"patch: expected exactly 1 _requires_bearer_auth anchor in {path}, found {count}",
        file=sys.stderr,
    )
    sys.exit(1)

source = source.replace(ANCHOR, REPLACEMENT, 1)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(source)

print(f"anthropic-bearer-for-agentgateway patch applied to {path}")
