#!/usr/bin/env python3
"""Vicegerent patch: let the auxiliary Anthropic client trust our in-cluster
gateway proxy, not just api.anthropic.com.

Context
-------
agent/auxiliary_client.py's _try_anthropic() builds the client used for
background/auxiliary calls (approval checks, title generation, memory
compression, vision fallback, etc). It reads model.base_url from
config.yaml, but only honors it when the host is in
_ANTHROPIC_COMPATIBLE_HOSTS — a hardcoded frozenset containing exactly
one hostname: "api.anthropic.com". This platform's model.base_url is
http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic
(agentgateway is the sealed egress-locked sandbox's single approved path
to any model API — see AGENTS.md "Environment" section). That hostname is
obviously never in the allowlist, so _is_anthropic_compatible_host()
always returns False for us, and _try_anthropic() silently discards our
configured base_url and falls back to _ANTHROPIC_DEFAULT_BASE_URL
("https://api.anthropic.com") instead.

Because direct egress to api.anthropic.com is sealed by design in this
sandbox (approved channels only: web_search, MCP servers, agentgateway,
git-over-SSH — see AGENTS.md "Limitations to expect"), every auxiliary
call that hits this fallback gets a bare httpx connection error, which
Hermes's own capacity-error classifier (_is_connection_error) correctly
treats as "provider unreachable" and fails over to the configured OpenAI
fallback (fallback_providers in config.yaml) — burning fallback capacity
and OpenAI quota on a problem that was never an Anthropic outage. Traced
live via agent.log: `provider=anthropic base_url=https://api.anthropic.com
... error_type=APIConnectionError` immediately followed by a fallback to
`custom/gpt-5.4`, at the exact moments auxiliary tasks (title_generation,
approval) ran.

The intent behind the allowlist (see upstream issue #52608, referenced in
the surrounding comment) is real and worth keeping: an operator who routes
their *main* session through a non-Anthropic host (e.g. OpenRouter) with
provider: anthropic set should not have that foreign host leak into
auxiliary calls. But a single literal hostname can't distinguish "OpenRouter
masquerading as Anthropic" (should stay blocked) from "our own trusted
gateway proxying to real Anthropic" (should be trusted) — and
hermes_cli/runtime_provider.py already solves exactly this ambiguity for
the *main* client via _anthropic_base_url_override_ok(), which additionally
trusts any URL whose path ends in /anthropic (the conventional suffix this
platform's own agentgateway route, and third-party Anthropic-compatible
proxies like Azure Foundry/MiniMax/Zhipu GLM/LiteLLM, all use). This patch
gives the auxiliary path the same host-OR-path-suffix trust rule so the two
resolution paths agree.

Fix
---
Extend _is_anthropic_compatible_host() to also trust a URL whose path
ends in "/anthropic" or "/anthropic/v1" (mirrors
hermes_cli.runtime_provider._detect_api_mode_for_url's suffix check,
already proven safe for the main client). The exact-hostname allowlist
stays as an additional (now redundant-but-harmless) fast path.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this function), the patch raises and the image build
fails, signalling a re-verify.

Once upstream unifies the main-client and auxiliary-client Anthropic-host
trust logic (or widens _ANTHROPIC_COMPATIBLE_HOSTS's own definition to
match _anthropic_base_url_override_ok), this patch can likely be dropped.
"""
import importlib.util
import sys

ANCHOR = (
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

REPLACEMENT = (
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

APPLIED_MARKER = "Vicegerent patch 0014"


def main() -> int:
    spec = importlib.util.find_spec("agent.auxiliary_client")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/auxiliary_client.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _is_anthropic_compatible_host anchor "
            f"in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: auxiliary Anthropic client now trusts /anthropic-suffixed "
        f"gateway proxies in {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
