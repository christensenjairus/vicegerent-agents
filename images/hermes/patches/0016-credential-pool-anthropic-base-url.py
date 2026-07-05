#!/usr/bin/env python3
"""Vicegerent patch: stop the persisted Anthropic credential-pool entry from
silently reintroducing the direct api.anthropic.com host that patches 0014
already fixed at the resolution layer.

Context
-------
Patch 0014 (this repo) made hermes_cli/runtime_provider.py and
agent/auxiliary_client.py both trust our in-cluster agentgateway /anthropic
proxy route via model.base_url. That resolution-layer fix is necessary but
not sufficient: a THIRD code path reintroduces the raw api.anthropic.com
host and silently overrides the gateway URL right before a subagent's
first API call.

agent/credential_pool.py's _seed_from_env() seeds a persisted pool entry
for provider "anthropic" whenever an API-key env var (ANTHROPIC_API_KEY /
ANTHROPIC_TOKEN / CLAUDE_CODE_OAUTH_TOKEN) is present. Its base_url
derivation is:

    base_url = env_url or pconfig.inference_base_url

...where env_url only comes from the ANTHROPIC_BASE_URL env var (a
DIFFERENT mechanism than model.base_url in config.yaml, which is how this
platform configures the agentgateway route) and pconfig.inference_base_url
is PROVIDER_REGISTRY["anthropic"].inference_base_url, hardcoded to
"https://api.anthropic.com". Since ANTHROPIC_BASE_URL is unset here, every
freshly-seeded (or re-seeded) anthropic pool entry gets
base_url="https://api.anthropic.com" baked in -- and because this pool is
persisted to the credential-pool file on the machine's persistent data
volume, that stale value survives pod restarts and even image rebuilds
that only touch code, not the persisted pool file.

tools/delegate_tool.py's _run_single_child() leases this pool's current
entry for every subagent and calls agent._swap_credential(leased_entry)
immediately before the child's first turn. run_agent.py's
_swap_credential() trusts the entry's base_url unconditionally:

    runtime_base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or self.base_url
    ...
    self._anthropic_base_url = runtime_base
    self.base_url = runtime_base

This overwrites the correctly-constructed gateway base_url (set by
_build_child_agent via the already-patched runtime_provider.py) with the
stale pool entry's "https://api.anthropic.com" right before the first
request -- reproduced live via agent.log on a freshly rebuilt pod with
0014 confirmed present and functionally correct in isolation: subagent
turns show base_url=https://api.anthropic.com / APIConnectionError
(blocked by the sealed egress boundary) immediately followed by fallback
to the OpenAI custom endpoint, which is separately quota-exhausted
(HTTP 429 insufficient_quota).

Fix
---
Two independent edits, closing both the seed-time and use-time windows for
this bug so a persisted-pool entry can never again reintroduce a raw
api.anthropic.com host once a gateway override is configured:

1. agent/credential_pool.py's _seed_from_env(): when provider ==
   "anthropic" and no explicit ANTHROPIC_BASE_URL override is present,
   consult hermes_cli.runtime_provider._resolve_anthropic_base_url_override()
   (the same single-source-of-truth helper 0014 introduced) before falling
   back to pconfig.inference_base_url. This ensures newly-seeded and
   re-seeded pool entries never persist a stale hardcoded host when
   model.base_url configures a trusted gateway route.
2. run_agent.py's _swap_credential(): when api_mode == "anthropic_messages"
   and self.provider == "anthropic", re-derive runtime_base through
   _resolve_anthropic_base_url_override() before falling back to the
   entry's raw base_url. This self-heals ALREADY-persisted stale entries
   (e.g. ones seeded before this patch existed) without requiring a manual
   pool-file wipe, since the check runs fresh on every lease instead of
   trusting whatever was written to disk at seed time.

Both edits import _resolve_anthropic_base_url_override lazily (inside the
function body) to avoid introducing a module-level import cycle between
agent/credential_pool.py, run_agent.py, and hermes_cli/runtime_provider.py
(runtime_provider.py already imports from agent.credential_pool at module
scope).

Fail-loud by design: if any anchor is absent or appears an unexpected
number of times in either file (upstream refactored it), the patch raises
and the image build fails, signalling a re-verify. The two file edits are
independent -- a failure in one is reported before the other is attempted,
and neither silently partially applies.

Depends on patch 0014 (must run after it, since it imports the helper
0014 introduces). Once upstream teaches the credential pool about
gateway-style Anthropic base_url overrides itself, this patch can likely
be dropped.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0016"

# ===========================================================================
# Part 1 — agent/credential_pool.py: don't seed a stale hardcoded host when
# a gateway override is configured
# ===========================================================================

CP_ANCHOR = (
    "        base_url = env_url or pconfig.inference_base_url\n"
    "        if provider == \"kimi-coding\":\n"
)

CP_REPLACEMENT = (
    "        base_url = env_url or pconfig.inference_base_url\n"
    "        if provider == \"anthropic\" and not env_url:\n"
    "            # Vicegerent patch 0016: don't persist a hardcoded\n"
    "            # api.anthropic.com host when model.base_url configures a\n"
    "            # trusted gateway route (e.g. this platform's agentgateway\n"
    "            # /anthropic proxy) -- consult the same single-source-of-truth\n"
    "            # helper 0014 introduced instead of pconfig.inference_base_url.\n"
    "            try:\n"
    "                from hermes_cli.runtime_provider import _resolve_anthropic_base_url_override\n"
    "                from hermes_cli.config import load_config as _load_cfg_for_seed\n"
    "                _model_cfg = (_load_cfg_for_seed() or {}).get(\"model\") or {}\n"
    "                _override = _resolve_anthropic_base_url_override(_model_cfg)\n"
    "                if _override:\n"
    "                    base_url = _override\n"
    "            except Exception:\n"
    "                pass\n"
    "        if provider == \"kimi-coding\":\n"
)

# ===========================================================================
# Part 2 — run_agent.py: self-heal already-persisted stale entries at lease
# time instead of trusting the raw pool entry field unconditionally
# ===========================================================================

RA_ANCHOR = (
    "    def _swap_credential(self, entry) -> None:\n"
    "        runtime_key = getattr(entry, \"runtime_api_key\", None) or getattr(entry, \"access_token\", \"\")\n"
    "        runtime_base = getattr(entry, \"runtime_base_url\", None) or getattr(entry, \"base_url\", None) or self.base_url\n"
    "\n"
    "        if self.api_mode == \"anthropic_messages\":\n"
)

RA_REPLACEMENT = (
    "    def _swap_credential(self, entry) -> None:\n"
    "        runtime_key = getattr(entry, \"runtime_api_key\", None) or getattr(entry, \"access_token\", \"\")\n"
    "        runtime_base = getattr(entry, \"runtime_base_url\", None) or getattr(entry, \"base_url\", None) or self.base_url\n"
    "\n"
    "        if self.api_mode == \"anthropic_messages\" and self.provider == \"anthropic\":\n"
    "            # Vicegerent patch 0016: a persisted pool entry may carry a\n"
    "            # stale hardcoded api.anthropic.com base_url seeded before a\n"
    "            # gateway override was configured (or before patch 0016\n"
    "            # existed). Re-derive through the same single-source-of-truth\n"
    "            # helper 0014 introduced so leasing a stale entry can never\n"
    "            # silently reintroduce a blocked direct-egress host.\n"
    "            try:\n"
    "                from hermes_cli.runtime_provider import _resolve_anthropic_base_url_override\n"
    "                from hermes_cli.config import load_config as _load_cfg_for_swap\n"
    "                _model_cfg = (_load_cfg_for_swap() or {}).get(\"model\") or {}\n"
    "                _override = _resolve_anthropic_base_url_override(_model_cfg)\n"
    "                if _override:\n"
    "                    runtime_base = _override\n"
    "            except Exception:\n"
    "                pass\n"
    "\n"
    "        if self.api_mode == \"anthropic_messages\":\n"
)


def _patch_credential_pool() -> None:
    spec = importlib.util.find_spec("agent.credential_pool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/credential_pool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return

    count = src.count(CP_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 seed-time base_url anchor in {path}, "
            f"found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(CP_ANCHOR, CP_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: anthropic pool entries no longer seed a stale hardcoded "
        f"api.anthropic.com host when a gateway override is configured, in {path}"
    )


def _patch_run_agent() -> None:
    spec = importlib.util.find_spec("run_agent")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate run_agent.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return

    count = src.count(RA_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _swap_credential anchor in {path}, "
            f"found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(RA_ANCHOR, RA_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: _swap_credential now self-heals a stale persisted anthropic "
        f"pool entry's base_url through the gateway override check, in {path}"
    )


def main() -> int:
    _patch_credential_pool()
    _patch_run_agent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
