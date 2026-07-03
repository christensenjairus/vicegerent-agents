#!/usr/bin/env python3
"""Vicegerent patch: don't register web_extract when only a search-only
backend is configured.

Context
-------
tools/web_tools.py registers both `web_search` and `web_extract` with the
same `check_fn=check_web_api_key`, which only asks "is *any* web backend
configured" (firecrawl, tavily, exa, parallel, searxng, brave-free, ddgs,
xai all count). SearXNG-only setups (this platform's `web.search_backend:
searxng`, see charts/agent/templates/_helpers.tpl) pass that check, so
`web_extract` gets registered and offered to the LLM — which then calls it,
gets a "SearXNG is a search-only backend and cannot extract URL content.
Set web.extract_backend to firecrawl, tavily, exa, or parallel." error, and
either burns a turn re-reading the error or (worse) doesn't know Tavily/
Firecrawl are reachable as separate MCP servers in this platform, not as
Hermes web-tool backends.

Root cause: `check_web_api_key()` doesn't distinguish extract-capable
backends from search-only ones. Each provider already exposes
`supports_extract()` (see plugins/web/*/provider.py — True for firecrawl/
tavily/exa/parallel, False for searxng/brave-free/ddgs/xai), but
`check_web_api_key()` never consults it.

Fix
---
Add `check_web_extract_capability()`: same availability logic as
`check_web_api_key()`, restricted to the four extract-capable backend
names. Wire it as `web_extract`'s own `check_fn` so the tool is not
registered at all in a SearXNG-only environment — no error message, no
wasted turn, `web_extract` simply doesn't appear in the toolset. Hermes
already renders this cleanly elsewhere (e.g. `hermes tools list` shows
"Tool not available: <reason>").

`web_search` keeps `check_web_api_key()` unchanged — SearXNG is a valid
search backend.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this registration), the patch raises and the image
build fails, signalling a re-verify.

Once upstream lands per-capability tool registration (tracked loosely
alongside hermes-agent #19198, the capability-based provider refactor),
this patch can likely be dropped — the refactor's own registration should
already gate `web_extract` on `supports_extract()`.
"""
import importlib.util
import sys

ANCHOR = (
    "registry.register(\n"
    "    name=\"web_extract\",\n"
    "    toolset=\"web\",\n"
    "    schema=WEB_EXTRACT_SCHEMA,\n"
    "    handler=lambda args, **kw: web_extract_tool(\n"
    "        args.get(\"urls\", [])[:5] if isinstance(args.get(\"urls\"), list) else [], \"markdown\"),\n"
    "    check_fn=check_web_api_key,\n"
    "    requires_env=_web_requires_env(),\n"
    "    is_async=True,\n"
    "    emoji=\"\U0001F4C4\",\n"
    "    max_result_size_chars=100_000,\n"
    ")\n"
)

_CHECK_FN_ANCHOR = (
    "# Convenience function to check Firecrawl credentials\n"
    "def check_web_api_key() -> bool:\n"
)

_NEW_CHECK_FN = (
    "# Vicegerent patch 0011: web_extract needs its own capability check.\n"
    "# check_web_api_key() below answers \"is ANY web backend configured\" and is\n"
    "# correct for web_search (searxng/brave-free/ddgs/xai are valid search\n"
    "# backends). web_extract additionally requires an extract-capable backend\n"
    "# (see plugins/web/*/provider.py supports_extract()) — searxng/brave-free/\n"
    "# ddgs/xai are search-only. Without this, web_extract registers whenever\n"
    "# *any* backend is configured, then fails at call time with a \"search-only\n"
    "# backend\" error instead of simply not existing as a tool.\n"
    "_EXTRACT_CAPABLE_BACKENDS = (\"firecrawl\", \"tavily\", \"exa\", \"parallel\")\n"
    "\n"
    "\n"
    "def check_web_extract_capability() -> bool:\n"
    "    \"\"\"Check whether an extract-capable web backend is available.\n"
    "\n"
    "    Mirrors check_web_api_key()'s logic but restricted to backends whose\n"
    "    provider.supports_extract() is True, so web_extract is not registered\n"
    "    in a search-only setup (e.g. SearXNG-only) — it simply doesn't appear\n"
    "    in the toolset rather than being offered and then erroring at call time.\n"
    "    \"\"\"\n"
    "    configured = _load_web_config().get(\"extract_backend\", \"\").lower().strip()\n"
    "    if not configured:\n"
    "        configured = _load_web_config().get(\"backend\", \"\").lower().strip()\n"
    "    if configured in _EXTRACT_CAPABLE_BACKENDS:\n"
    "        return _is_backend_available(configured)\n"
    "    if configured:\n"
    "        # Explicit backend configured but it's search-only (or unrecognized)\n"
    "        # for extraction purposes — don't fall through to the generic scan,\n"
    "        # that would register web_extract against operator intent.\n"
    "        return False\n"
    "    return any(\n"
    "        _is_backend_available(backend) for backend in _EXTRACT_CAPABLE_BACKENDS\n"
    "    )\n"
    "\n"
    "\n"
    "# Convenience function to check Firecrawl credentials\n"
    "def check_web_api_key() -> bool:\n"
)

REPLACEMENT = ANCHOR.replace(
    "check_fn=check_web_api_key,", "check_fn=check_web_extract_capability,"
)

APPLIED_MARKER = "Vicegerent patch 0011"


def main() -> int:
    spec = importlib.util.find_spec("tools.web_tools")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/web_tools.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count_reg = src.count(ANCHOR)
    if count_reg != 1:
        raise SystemExit(
            f"patch: expected exactly 1 web_extract registration anchor in {path}, "
            f"found {count_reg} (upstream drifted — re-verify)"
        )
    count_fn = src.count(_CHECK_FN_ANCHOR)
    if count_fn != 1:
        raise SystemExit(
            f"patch: expected exactly 1 check_web_api_key anchor in {path}, "
            f"found {count_fn} (upstream drifted — re-verify)"
        )

    src = src.replace(_CHECK_FN_ANCHOR, _NEW_CHECK_FN, 1)
    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: web_extract now gated on extract-capable backend in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
