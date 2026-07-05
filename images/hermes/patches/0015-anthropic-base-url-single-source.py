#!/usr/bin/env python3
"""Vicegerent patch: factor the triple-duplicated 'is model.base_url a valid
Anthropic override' resolution in hermes_cli/runtime_provider.py into one
function, so the three copies can never silently desync.

Context
-------
_anthropic_base_url_override_ok(url) -> bool already exists as the single
canonical "is this URL plausibly an Anthropic-compatible endpoint" check.
But the *caller* logic that surrounds it --

    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
    cfg_base_url = ""
    if cfg_provider == "anthropic":
        cfg_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
        if not _anthropic_base_url_override_ok(cfg_base_url):
            cfg_base_url = ""

-- is copy-pasted verbatim in THREE separate places in this file:
  1. _resolve_runtime_from_pool_entry() (credential-pool path)
  2. _resolve_explicit_runtime()        (explicit override path)
  3. resolve_runtime_provider() main body (env-var/fallback path)

All three currently agree and correctly resolve to this platform's
agentgateway /anthropic route. But nothing enforces that agreement: a
future edit to any one copy (an Azure quirk fix, a new host-matching rule)
can silently desync it from the other two. Since delegate_task's
credential resolution (_resolve_delegation_credentials in
tools/delegate_tool.py) can route through ANY of these three paths
depending on whether a credential pool is populated, a desync here is
exactly the kind of bug that would surface as "subagents intermittently
call https://api.anthropic.com directly instead of the gateway" --
non-deterministic, hard to repro, and easy to reintroduce even after
fixing one instance.

Fix
---
Add one helper, _resolve_anthropic_base_url_override(model_cfg, fallback),
that encapsulates the cfg_provider/cfg_base_url/override_ok logic exactly
once, and rewrite all three call sites to use it. Behavior is identical
(same inputs, same outputs) -- this is a refactor for correctness
durability, not a behavior change.

Fail-loud by design: if any anchor is missing or appears an unexpected
number of times (upstream refactored this file), the patch raises and the
image build fails, signalling a re-verify.
"""
import re
import sys

PATH = "/opt/hermes/hermes_cli/runtime_provider.py"

HELPER_ANCHOR = (
    "def _anthropic_base_url_override_ok(base_url: str) -> bool:\n"
)

# The three duplicated blocks, each with a different final base_url fallback
# expression -- captured as separate anchors so each is replaced precisely.

BLOCK_POOL_ENTRY = (
    "    elif provider == \"anthropic\":\n"
    "        api_mode = \"anthropic_messages\"\n"
    "        cfg_provider = str(model_cfg.get(\"provider\") or \"\").strip().lower()\n"
    "        cfg_base_url = \"\"\n"
    "        if cfg_provider == \"anthropic\":\n"
    "            cfg_base_url = str(model_cfg.get(\"base_url\") or \"\").strip().rstrip(\"/\")\n"
    "            if not _anthropic_base_url_override_ok(cfg_base_url):\n"
    "                cfg_base_url = \"\"\n"
    "        base_url = cfg_base_url or base_url or \"https://api.anthropic.com\"\n"
)
BLOCK_POOL_ENTRY_REPLACEMENT = (
    "    elif provider == \"anthropic\":\n"
    "        api_mode = \"anthropic_messages\"\n"
    "        base_url = (\n"
    "            _resolve_anthropic_base_url_override(model_cfg)\n"
    "            or base_url\n"
    "            or \"https://api.anthropic.com\"\n"
    "        )\n"
)

BLOCK_EXPLICIT_RUNTIME = (
    "    if provider == \"anthropic\":\n"
    "        cfg_provider = str(model_cfg.get(\"provider\") or \"\").strip().lower()\n"
    "        cfg_base_url = \"\"\n"
    "        if cfg_provider == \"anthropic\":\n"
    "            cfg_base_url = str(model_cfg.get(\"base_url\") or \"\").strip().rstrip(\"/\")\n"
    "            if not _anthropic_base_url_override_ok(cfg_base_url):\n"
    "                cfg_base_url = \"\"\n"
    "        base_url = explicit_base_url or cfg_base_url or \"https://api.anthropic.com\"\n"
)
BLOCK_EXPLICIT_RUNTIME_REPLACEMENT = (
    "    if provider == \"anthropic\":\n"
    "        base_url = (\n"
    "            explicit_base_url\n"
    "            or _resolve_anthropic_base_url_override(model_cfg)\n"
    "            or \"https://api.anthropic.com\"\n"
    "        )\n"
)

BLOCK_MAIN_BODY = (
    "    # Anthropic (native Messages API)\n"
    "    if provider == \"anthropic\":\n"
    "        # Allow base URL override from config.yaml model.base_url, but only\n"
    "        # when the configured provider is anthropic — otherwise a non-Anthropic\n"
    "        # base_url (e.g. Codex endpoint) would leak into Anthropic requests.\n"
    "        cfg_provider = str(model_cfg.get(\"provider\") or \"\").strip().lower()\n"
    "        cfg_base_url = \"\"\n"
    "        if cfg_provider == \"anthropic\":\n"
    "            cfg_base_url = (model_cfg.get(\"base_url\") or \"\").strip().rstrip(\"/\")\n"
    "            if not _anthropic_base_url_override_ok(cfg_base_url):\n"
    "                cfg_base_url = \"\"\n"
    "        base_url = cfg_base_url or \"https://api.anthropic.com\"\n"
)
BLOCK_MAIN_BODY_REPLACEMENT = (
    "    # Anthropic (native Messages API)\n"
    "    if provider == \"anthropic\":\n"
    "        # Allow base URL override from config.yaml model.base_url, but only\n"
    "        # when the configured provider is anthropic — otherwise a non-Anthropic\n"
    "        # base_url (e.g. Codex endpoint) would leak into Anthropic requests.\n"
    "        # (Vicegerent patch 0015: factored into _resolve_anthropic_base_url_override\n"
    "        # so this logic can't silently desync from the other two call sites.)\n"
    "        base_url = _resolve_anthropic_base_url_override(model_cfg) or \"https://api.anthropic.com\"\n"
)

HELPER_FUNCTION = (
    "def _resolve_anthropic_base_url_override(model_cfg) -> str:\n"
    "    \"\"\"Vicegerent patch 0015: single source of truth for 'does model.base_url\n"
    "    back native Anthropic resolution'.\n"
    "\n"
    "    Factored out of three previously-duplicated call sites (credential-pool\n"
    "    path, explicit-override path, env-var/fallback path) so a future edit to\n"
    "    the override-check logic can't apply to only some of them and silently\n"
    "    desync which resolution path trusts the configured base_url. Returns the\n"
    "    trimmed base_url string when model.provider == 'anthropic' and the URL\n"
    "    passes _anthropic_base_url_override_ok(); returns '' otherwise, so callers\n"
    "    can `... or <fallback>` uniformly.\n"
    "    \"\"\"\n"
    "    cfg_provider = str(model_cfg.get(\"provider\") or \"\").strip().lower()\n"
    "    if cfg_provider != \"anthropic\":\n"
    "        return \"\"\n"
    "    cfg_base_url = str(model_cfg.get(\"base_url\") or \"\").strip().rstrip(\"/\")\n"
    "    if not _anthropic_base_url_override_ok(cfg_base_url):\n"
    "        return \"\"\n"
    "    return cfg_base_url\n"
    "\n"
    "\n"
)


def main() -> int:
    with open(PATH, "r", encoding="utf-8") as f:
        content = f.read()

    if content.count(HELPER_ANCHOR) != 1:
        print(
            f"FATAL: expected exactly 1 occurrence of helper anchor, "
            f"found {content.count(HELPER_ANCHOR)}. Upstream file changed; re-verify patch 0015.",
            file=sys.stderr,
        )
        return 1

    blocks = [
        ("pool entry", BLOCK_POOL_ENTRY, BLOCK_POOL_ENTRY_REPLACEMENT),
        ("explicit runtime", BLOCK_EXPLICIT_RUNTIME, BLOCK_EXPLICIT_RUNTIME_REPLACEMENT),
        ("main body", BLOCK_MAIN_BODY, BLOCK_MAIN_BODY_REPLACEMENT),
    ]
    for name, anchor, replacement in blocks:
        count = content.count(anchor)
        if count != 1:
            print(
                f"FATAL: expected exactly 1 occurrence of the '{name}' block, "
                f"found {count}. Upstream file changed; re-verify patch 0015.",
                file=sys.stderr,
            )
            return 1
        content = content.replace(anchor, replacement)

    # Insert the new helper function immediately after _anthropic_base_url_override_ok's
    # definition ends -- i.e. right before the next top-level `def`. Find the anchor
    # function's end by locating the following "def _auto_detect_local_model" anchor,
    # which is defined immediately after it in the current file layout.
    NEXT_DEF_ANCHOR = "def _auto_detect_local_model(base_url: str) -> str:\n"
    if content.count(NEXT_DEF_ANCHOR) != 1:
        print(
            f"FATAL: expected exactly 1 occurrence of insertion-point anchor "
            f"'{NEXT_DEF_ANCHOR.strip()}', found {content.count(NEXT_DEF_ANCHOR)}. "
            f"Upstream file changed; re-verify patch 0015.",
            file=sys.stderr,
        )
        return 1
    content = content.replace(NEXT_DEF_ANCHOR, HELPER_FUNCTION + NEXT_DEF_ANCHOR)

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print("Patch 0015 applied: factored Anthropic base_url override resolution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
