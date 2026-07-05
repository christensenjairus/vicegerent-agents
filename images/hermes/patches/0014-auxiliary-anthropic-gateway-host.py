#!/usr/bin/env python3
"""Vicegerent patch: trust our in-cluster agentgateway proxy for Anthropic
traffic in BOTH the auxiliary client and the main/subagent runtime resolver,
and de-duplicate the main resolver's repeated override-check logic while
we're in that file.

Context
-------
Two separate Hermes modules each decide whether a configured
``model.base_url`` is trusted for native Anthropic (Messages API) traffic:

1. ``agent/auxiliary_client.py``'s ``_try_anthropic()`` builds the client
   used for background/auxiliary calls (approval checks, title generation,
   memory compression, vision fallback, etc). It reads ``model.base_url``
   from config.yaml, but only honors it when the host is in
   ``_ANTHROPIC_COMPATIBLE_HOSTS`` — a hardcoded frozenset containing
   exactly one hostname: ``api.anthropic.com``. This platform's
   ``model.base_url`` is
   ``http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic``
   (agentgateway is the sealed egress-locked sandbox's single approved path
   to any model API — see AGENTS.md "Environment" section). That hostname
   is obviously never in the allowlist, so
   ``_is_anthropic_compatible_host()`` always returns False for us, and
   ``_try_anthropic()`` silently discards our configured base_url and
   falls back to ``_ANTHROPIC_DEFAULT_BASE_URL`` (``https://api.anthropic.com``)
   instead.

2. ``hermes_cli/runtime_provider.py`` resolves credentials for the MAIN
   agent loop and every ``delegate_task()`` subagent. It already has the
   right idea — ``_anthropic_base_url_override_ok(url)`` additionally
   trusts any URL whose path ends in ``/anthropic`` — but the *caller*
   logic around that check (``cfg_provider``/``cfg_base_url`` derivation +
   the override-ok gate) is copy-pasted verbatim in THREE separate places:
   ``_resolve_runtime_from_pool_entry()`` (credential-pool path),
   ``_resolve_explicit_runtime()`` (explicit-override path), and
   ``resolve_runtime_provider()``'s main body (env-var/fallback path).
   Nothing enforces that the three copies agree; a future edit to any one
   (an Azure quirk fix, a new host-matching rule) can silently desync it
   from the other two. Since ``delegate_task``'s credential resolution
   (``_resolve_delegation_credentials`` in ``tools/delegate_tool.py``) can
   route through ANY of these three paths depending on whether a
   credential pool is populated, a desync here is exactly the kind of bug
   that surfaces as "subagents intermittently call
   https://api.anthropic.com directly instead of the gateway" —
   non-deterministic, hard to repro, and easy to reintroduce even after
   fixing one instance.

Because direct egress to ``api.anthropic.com`` is sealed by design in this
sandbox (approved channels only: web_search, MCP servers, agentgateway,
git-over-SSH — see AGENTS.md "Limitations to expect"), every call that
falls back to the bare hostname default gets an httpx connection error,
which Hermes's own capacity-error classifier (``_is_connection_error``)
correctly but confusingly treats as "provider unreachable" and fails over
to the configured OpenAI fallback (``fallback_providers`` in config.yaml)
— burning fallback capacity and OpenAI quota on a problem that was never
an Anthropic outage. Traced live via agent.log on both paths:
- Auxiliary: `provider=anthropic base_url=https://api.anthropic.com
  ... error_type=APIConnectionError` at title_generation/approval moments,
  immediately followed by a fallback to `custom/gpt-5.4`.
- Main/subagent: the same signature on ``delegate_task()`` subagent turns,
  immediately followed by `Fallback activated: claude-sonnet-5 -> gpt-5.4
  (custom)`, then the fallback itself exhausting OpenAI's quota (HTTP 429
  `insufficient_quota`) — the subagent never got as far as taking the
  gateway route to begin with.

The intent behind the auxiliary-client allowlist (see upstream issue
#52608, referenced in the surrounding comment) is real and worth keeping:
an operator who routes their *main* session through a non-Anthropic host
(e.g. OpenRouter) with ``provider: anthropic`` set should not have that
foreign host leak into auxiliary calls. But a single literal hostname
can't distinguish "OpenRouter masquerading as Anthropic" (should stay
blocked) from "our own trusted gateway proxying to real Anthropic"
(should be trusted) — exactly the ambiguity ``runtime_provider.py``
already solves for the main client via
``_anthropic_base_url_override_ok()``, which trusts any URL whose path
ends in ``/anthropic`` (the conventional suffix this platform's own
agentgateway route, and third-party Anthropic-compatible proxies like
Azure Foundry/MiniMax/Zhipu GLM/LiteLLM, all use).

Fix
---
Two independent, non-overlapping edits applied by one script, since both
close the same underlying gap (gateway-host trust for Anthropic traffic)
and are logically one change:

1. ``agent/auxiliary_client.py``: extend ``_is_anthropic_compatible_host()``
   to also trust a URL whose path ends in ``/anthropic`` or
   ``/anthropic/v1`` (mirrors ``runtime_provider``'s existing suffix
   check). The exact-hostname allowlist stays as an additional
   (now redundant-but-harmless) fast path.
2. ``hermes_cli/runtime_provider.py``: add one helper,
   ``_resolve_anthropic_base_url_override(model_cfg)``, that encapsulates
   the ``cfg_provider``/``cfg_base_url``/``override_ok`` logic exactly
   once, and rewrite all three call sites to use it. Behavior is
   identical (same inputs, same outputs) — this is a refactor for
   correctness durability, not a behavior change.

Fail-loud by design: if any anchor is absent or appears an unexpected
number of times in either file (upstream refactored it), the patch raises
and the image build fails, signalling a re-verify. The two file edits are
independent — a failure in one is reported before the other is attempted,
and neither silently partially applies.

Once upstream unifies the auxiliary-client and main-client Anthropic-host
trust logic (or widens ``_ANTHROPIC_COMPATIBLE_HOSTS``'s own definition to
match ``_anthropic_base_url_override_ok``, and de-duplicates
runtime_provider.py's three call sites itself), this patch can likely be
dropped.
"""
import importlib.util
import sys

# ===========================================================================
# Part 1 — agent/auxiliary_client.py: gateway-host trust for aux calls
# ===========================================================================

AUX_APPLIED_MARKER = "Vicegerent patch 0014"

AUX_ANCHOR = (
    "def _is_anthropic_compatible_host(url: str) -> bool:\n"
    "    \"\"\"Return True if ``url``'s hostname is an Anthropic endpoint we trust for aux calls.\"\"\"\n"
    "    if not url:\n"
    "        return False\n"
    "    try:\n"
    "        from urllib.parse import urlparse\n"
    "        host = (urlparse(url).hostname or \"\").strip().lower().rstrip(\".\")\n"
    "        return host in _ANTHROPIC_COMPATIBLE_HOSTS\n"
    "    except Exception:\n"
    "        return False\n"
)

AUX_REPLACEMENT = (
    "def _is_anthropic_compatible_host(url: str) -> bool:\n"
    "    \"\"\"Return True if ``url`` is an Anthropic endpoint we trust for aux calls.\n"
    "\n"
    "    Vicegerent patch 0014: trust either an exact hostname match against\n"
    "    _ANTHROPIC_COMPATIBLE_HOSTS, or a path ending in /anthropic (or\n"
    "    /anthropic/v1) — the conventional suffix used by our own in-cluster\n"
    "    agentgateway proxy route and by third-party Anthropic-compatible\n"
    "    proxies (Azure Foundry, MiniMax, Zhipu GLM, LiteLLM). Mirrors the\n"
    "    suffix check hermes_cli.runtime_provider._detect_api_mode_for_url()\n"
    "    already applies for the main client's Anthropic host trust decision\n"
    "    (_anthropic_base_url_override_ok), so both resolution paths agree.\n"
    "    \"\"\"\n"
    "    if not url:\n"
    "        return False\n"
    "    try:\n"
    "        from urllib.parse import urlparse\n"
    "        parsed = urlparse(url)\n"
    "        host = (parsed.hostname or \"\").strip().lower().rstrip(\".\")\n"
    "        if host in _ANTHROPIC_COMPATIBLE_HOSTS:\n"
    "            return True\n"
    "        path = parsed.path.rstrip(\"/\").lower()\n"
    "        return path.endswith(\"/anthropic\") or path.endswith(\"/anthropic/v1\")\n"
    "    except Exception:\n"
    "        return False\n"
)


def _patch_auxiliary_client() -> None:
    spec = importlib.util.find_spec("agent.auxiliary_client")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/auxiliary_client.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if AUX_APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return

    count = src.count(AUX_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _is_anthropic_compatible_host anchor "
            f"in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(AUX_ANCHOR, AUX_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: auxiliary Anthropic client now trusts /anthropic-suffixed "
        f"gateway proxies in {path}"
    )


# ===========================================================================
# Part 2 — hermes_cli/runtime_provider.py: factor the triple-duplicated
# "is model.base_url a valid Anthropic override" resolution into one
# function, so the three copies can never silently desync.
# ===========================================================================

RP_HELPER_ANCHOR = (
    "def _anthropic_base_url_override_ok(base_url: str) -> bool:\n"
)

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
    "        # (Vicegerent patch 0014: factored into _resolve_anthropic_base_url_override\n"
    "        # so this logic can't silently desync from the other two call sites.)\n"
    "        base_url = _resolve_anthropic_base_url_override(model_cfg) or \"https://api.anthropic.com\"\n"
)

RP_HELPER_FUNCTION = (
    "def _resolve_anthropic_base_url_override(model_cfg) -> str:\n"
    "    \"\"\"Vicegerent patch 0014: single source of truth for 'does model.base_url\n"
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

RP_NEXT_DEF_ANCHOR = "def _auto_detect_local_model(base_url: str) -> str:\n"


def _patch_runtime_provider() -> None:
    spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/runtime_provider.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if RP_HELPER_FUNCTION.split("\n", 1)[0] in content:
        # Helper already inserted -> whole patch already applied.
        print(f"patch: already applied to {path} — no-op")
        return

    if content.count(RP_HELPER_ANCHOR) != 1:
        raise SystemExit(
            f"patch: expected exactly 1 occurrence of helper anchor in {path}, "
            f"found {content.count(RP_HELPER_ANCHOR)} (upstream drifted — re-verify)"
        )

    blocks = [
        ("pool entry", BLOCK_POOL_ENTRY, BLOCK_POOL_ENTRY_REPLACEMENT),
        ("explicit runtime", BLOCK_EXPLICIT_RUNTIME, BLOCK_EXPLICIT_RUNTIME_REPLACEMENT),
        ("main body", BLOCK_MAIN_BODY, BLOCK_MAIN_BODY_REPLACEMENT),
    ]
    for name, anchor, replacement in blocks:
        count = content.count(anchor)
        if count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 occurrence of the '{name}' block in {path}, "
                f"found {count} (upstream drifted — re-verify)"
            )
        content = content.replace(anchor, replacement)

    # Insert the new helper function immediately after
    # _anthropic_base_url_override_ok's definition ends -- i.e. right before
    # the next top-level `def`, which in the current file layout is
    # _auto_detect_local_model.
    if content.count(RP_NEXT_DEF_ANCHOR) != 1:
        raise SystemExit(
            f"patch: expected exactly 1 occurrence of insertion-point anchor "
            f"'{RP_NEXT_DEF_ANCHOR.strip()}' in {path}, found "
            f"{content.count(RP_NEXT_DEF_ANCHOR)} (upstream drifted — re-verify)"
        )
    content = content.replace(RP_NEXT_DEF_ANCHOR, RP_HELPER_FUNCTION + RP_NEXT_DEF_ANCHOR)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    compile(content, path, "exec")
    print(
        f"patch: main/subagent Anthropic runtime resolver now shares "
        f"_resolve_anthropic_base_url_override across all 3 call sites in {path}"
    )


def main() -> int:
    _patch_auxiliary_client()
    _patch_runtime_provider()
    return 0


if __name__ == "__main__":
    sys.exit(main())
