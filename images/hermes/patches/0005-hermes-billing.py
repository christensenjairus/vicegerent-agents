#!/usr/bin/env python3
"""Vicegerent patches: fix Hermes cost accounting for agentgateway-proxied providers.

Three fixes bundled here — all address the same broken billing chain and should
be dropped together once Hermes supports a billing_provider field in the provider
config (allowing per-provider billing identity override in config.yaml).

BACKGROUND
----------
In the vicegerent sandbox, Hermes providers are named after the agentgateway route
(sonnet, haiku, opus, gpt-4-1, gpt-4o-mini, gpt-5-5). Hermes doesn't know these
names as canonical billing identities, so the cost accounting chain falls through to
billing_provider='custom' / cost_status='unknown' / NULL estimated_cost_usd.

THE CHAIN (root cause -> symptom):

  1. _resolve_named_custom_runtime() [runtime_provider.py]
     Returns {"provider": "custom", ...} for ALL named custom providers.
     -> agent.provider = "custom" -> billing_provider = "custom" in state.db
     ROOT CAUSE. Fixes 2+3 are belt-and-suspenders on top of this.

  2. resolve_provider("sonnet") [auth.py]
     "sonnet" not in _PROVIDER_ALIASES -> AuthError -> caller falls back to "custom".

  3. resolve_billing_route(provider="custom") [usage_pricing.py]
     "custom" hits the unknown branch -> billing_mode="unknown".
"""
import importlib.util
import sys


# ---------------------------------------------------------------------------
# Fix 1: runtime_provider.py -- preserve route name as provider (root cause)
# ---------------------------------------------------------------------------
#
# _resolve_named_custom_runtime() named-custom-provider path (the _get_named_custom_provider
# branch) -- NOT the direct-alias path at the top of the function which stays "custom".
#
# is_custom_provider (chat_completion_helpers.py:797) checks agent.provider == "custom"
# but is only declared in a TypedDict, never used in logic -- safe to change.

_RT_ANCHOR = (
    '    result = {\n'
    '        "provider": "custom",\n'
    '        "api_mode": custom_provider.get("api_mode")\n'
)

_RT_REPLACEMENT = (
    '    result = {\n'
    '        # vicegerent: use requested_provider (e.g. "sonnet") not "custom" so\n'
    '        # agent.provider carries the route name into billing_provider in state.db.\n'
    '        "provider": requested_provider,\n'
    '        "api_mode": custom_provider.get("api_mode")\n'
)

_RT_MARKER = '# vicegerent: use requested_provider (e.g. "sonnet") not "custom"'


def _patch_runtime_provider() -> None:
    spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli.runtime_provider module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _RT_MARKER in src:
        print(f"patch(runtime-provider): already applied to {path} -- no-op")
        return

    count = src.count(_RT_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(runtime-provider): expected 1 anchor in {path}, found {count} "
            "(Hermes upstream changed _resolve_named_custom_runtime -- re-verify)"
        )

    src = src.replace(_RT_ANCHOR, _RT_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")

    with open(path, "r", encoding="utf-8") as f:
        written = f.read()
    if _RT_MARKER not in written:
        raise SystemExit(f"patch(runtime-provider): marker missing after write in {path}")

    print(f"patch(runtime-provider): _resolve_named_custom_runtime fixed in {path}")


# ---------------------------------------------------------------------------
# Fix 2: auth.py -- add route-name aliases to resolve_provider()
# ---------------------------------------------------------------------------

_ALIASES_TO_ADD = {
    "sonnet":      "anthropic",
    "haiku":       "anthropic",
    "opus":        "anthropic",
    "haiku-oai":   "anthropic",
    "gpt-5-5":     "openai-api",
    "gpt-4-1":     "openai-api",
    "gpt-4o-mini": "openai-api",
}

_AUTH_ANCHOR = (
    '        "vllm": "custom", "llamacpp": "custom",\n'
    '        "llama.cpp": "custom", "llama-cpp": "custom",\n'
    '    }\n'
)

_AUTH_REPLACEMENT = (
    '        "vllm": "custom", "llamacpp": "custom",\n'
    '        "llama.cpp": "custom", "llama-cpp": "custom",\n'
    '        # vicegerent: agentgateway route-name -> canonical billing provider\n'
    + "".join(f'        "{k}": "{v}",\n' for k, v in _ALIASES_TO_ADD.items())
    + '    }\n'
)

_AUTH_MARKER = '"sonnet": "anthropic"'


def _patch_auth() -> None:
    spec = importlib.util.find_spec("hermes_cli.auth")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli.auth module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _AUTH_MARKER in src:
        print(f"patch(auth): already applied to {path} -- no-op")
        return

    count = src.count(_AUTH_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(auth): expected 1 anchor in {path}, found {count} "
            "(Hermes upstream changed auth.py resolve_provider aliases -- re-verify)"
        )

    src = src.replace(_AUTH_ANCHOR, _AUTH_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")

    with open(path, "r", encoding="utf-8") as f:
        written = f.read()
    if _AUTH_MARKER not in written:
        raise SystemExit(f"patch(auth): marker missing after write in {path}")

    print(f"patch(auth): added {len(_ALIASES_TO_ADD)} agentgateway aliases to resolve_provider() in {path}")


# ---------------------------------------------------------------------------
# Fix 3: usage_pricing.py -- map route names in resolve_billing_route()
# ---------------------------------------------------------------------------

_ANTHROPIC_ROUTES = {"sonnet", "haiku", "opus", "haiku-oai"}
_OPENAI_ROUTES = {"gpt-5-5", "gpt-4-1", "gpt-4o-mini", "openai-api"}

_PRICING_ANCHOR = (
    '    if provider_name in {"custom", "local"} or (base and "localhost" in base):\n'
    '        return BillingRoute(provider=provider_name or "custom", model=model, base_url=base_url or "", billing_mode="unknown")\n'
)

_PRICING_REPLACEMENT = (
    '    # vicegerent: map agentgateway route-name providers to upstream billing identity\n'
    '    if provider_name in _VICEGERENT_ANTHROPIC_ROUTES:\n'
    '        return BillingRoute(provider="anthropic", model=model, base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    '    if provider_name in _VICEGERENT_OPENAI_ROUTES:\n'
    '        return BillingRoute(provider="openai", model=model, base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    '    if provider_name in {"custom", "local"} or (base and "localhost" in base):\n'
    '        return BillingRoute(provider=provider_name or "custom", model=model, base_url=base_url or "", billing_mode="unknown")\n'
)

_PRICING_MARKER = "_VICEGERENT_ANTHROPIC_ROUTES"


def _patch_usage_pricing() -> None:
    spec = importlib.util.find_spec("agent.usage_pricing")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent.usage_pricing module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _PRICING_MARKER in src:
        print(f"patch(usage-pricing): already applied to {path} -- no-op")
        return

    count = src.count(_PRICING_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(usage-pricing): expected 1 anchor in {path}, found {count} "
            "(Hermes upstream changed usage_pricing.py resolve_billing_route -- re-verify)"
        )

    fn_anchor = "def resolve_billing_route(\n"
    if fn_anchor not in src:
        raise SystemExit(f"patch(usage-pricing): function anchor not found in {path}")

    sets_block = (
        f"# vicegerent: agentgateway route-name -> upstream billing provider\n"
        f"_VICEGERENT_ANTHROPIC_ROUTES = {_ANTHROPIC_ROUTES!r}\n"
        f"_VICEGERENT_OPENAI_ROUTES = {_OPENAI_ROUTES!r}\n"
        f"\n"
    )
    src = src.replace(fn_anchor, sets_block + fn_anchor, 1)
    src = src.replace(_PRICING_ANCHOR, _PRICING_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(usage-pricing): vicegerent billing route mapping applied to {path}")


# ---------------------------------------------------------------------------

def main() -> int:
    _patch_runtime_provider()
    _patch_auth()
    _patch_usage_pricing()
    return 0


if __name__ == "__main__":
    sys.exit(main())
