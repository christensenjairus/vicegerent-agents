#!/usr/bin/env python3
"""Vicegerent patch: fix cost accounting for agentgateway-proxied providers.

In the vicegerent sandbox, Hermes provider config keys are named "sonnet",
"opus", "haiku", "gpt-5-5", "gpt-4-1", "gpt-4o-mini" (the agentgateway
route names). Hermes passes these as the `provider` arg to estimate_usage_cost.

resolve_billing_route() in usage_pricing.py only recognises "anthropic" and
"openai" as billable provider names; unrecognised names fall through to
billing_mode="unknown" → cost_status="unknown" → NULL in state.db.

This patch adds a vicegerent-specific branch that maps our agentgateway route
names to their upstream billing provider so cost is computed correctly.

Remove once Hermes supports a billing_provider field in the provider config
(allowing per-provider billing identity override in config.yaml).
"""
import importlib.util
import sys

# Provider keys used in vicegerent config.yaml → upstream billing provider
_VICEGERENT_ANTHROPIC_PROVIDERS = {"sonnet", "haiku", "opus"}
_VICEGERENT_OPENAI_PROVIDERS = {"gpt-5-5", "gpt-4-1", "gpt-4o-mini"}

ANCHOR = (
    '    if provider_name in {"custom", "local"} or (base and "localhost" in base):\n'
    '        return BillingRoute(provider=provider_name or "custom", model=model, base_url=base_url or "", billing_mode="unknown")\n'
)

REPLACEMENT = (
    '    # vicegerent: map agentgateway route-name providers to upstream billing identity\n'
    '    if provider_name in _VICEGERENT_ANTHROPIC_PROVIDERS:\n'
    '        return BillingRoute(provider="anthropic", model=model, base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    '    if provider_name in _VICEGERENT_OPENAI_PROVIDERS:\n'
    '        return BillingRoute(provider="openai", model=model, base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    '    if provider_name in {"custom", "local"} or (base and "localhost" in base):\n'
    '        return BillingRoute(provider=provider_name or "custom", model=model, base_url=base_url or "", billing_mode="unknown")\n'
)

APPLIED_MARKER = "_VICEGERENT_ANTHROPIC_PROVIDERS"


def main() -> int:
    spec = importlib.util.find_spec("agent.usage_pricing")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent.usage_pricing module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {path}, found {count} "
            "(Hermes upstream changed usage_pricing.py resolve_billing_route — re-verify)"
        )

    # Inject the provider sets at module level, before the function that uses them.
    # Insert just before resolve_billing_route.
    fn_anchor = "def resolve_billing_route(\n"
    if fn_anchor not in src:
        raise SystemExit(f"patch: function anchor {fn_anchor!r} not found in {path}")

    sets_block = (
        f"# vicegerent: agentgateway route-name → upstream billing provider mapping\n"
        f"_VICEGERENT_ANTHROPIC_PROVIDERS = {_VICEGERENT_ANTHROPIC_PROVIDERS!r}\n"
        f"_VICEGERENT_OPENAI_PROVIDERS = {_VICEGERENT_OPENAI_PROVIDERS!r}\n"
        f"\n"
    )
    src = src.replace(fn_anchor, sets_block + fn_anchor, 1)
    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: vicegerent billing route mapping applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
