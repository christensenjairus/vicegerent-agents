#!/usr/bin/env python3
"""Vicegerent patch: inject GLM pricing into Hermes's usage_pricing.

Hermes's _OFFICIAL_DOCS_PRICING dict snapshots provider pricing at the
time the code is cut. GLM models from Z.ai (provider ``zai``, base URL via
agentgateway ``/glm`` route) aren't in that snapshot, and
resolve_billing_route() has no ``zai`` branch, so every GLM session
records cost_status='unknown' and the Slack runtime footer never shows a
cost line.

Two changes:
1. resolve_billing_route() — new ``zai``/``glm`` branch returning
   billing_mode='official_docs_snapshot', inserted after the existing
   minimax/minimax-cn branch (same pattern — third-party
   OpenAI-compatible API).
2. _OFFICIAL_DOCS_PRICING — add glm-5.2, glm-5.1, and glm-5 entries
   with official API pricing from https://docs.z.ai/guides/overview/pricing.

Remove once upstream Hermes adds GLM pricing to usage_pricing.py itself.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0038"

# ---------------------------------------------------------------------------
# Fix 1: resolve_billing_route() — add zai/glm branch
# ---------------------------------------------------------------------------
# Anchor: the minimax/minimax-cn branch plus the comment line that follows it.

_ROUTE_ANCHOR = (
    '    if provider_name in {"minimax", "minimax-cn"}:\n'
    "        return BillingRoute(provider=provider_name, model=model.split(\"/\")[-1], base_url=base_url or \"\", billing_mode=\"official_docs_snapshot\")\n"
    "    # Vertex AI hosts the same Gemini models as Google AI Studio; price them\n"
)

_ROUTE_REPLACEMENT = (
    '    if provider_name in {"minimax", "minimax-cn"}:\n'
    "        return BillingRoute(provider=provider_name, model=model.split(\"/\")[-1], base_url=base_url or \"\", billing_mode=\"official_docs_snapshot\")\n"
    "    # Vicegerent patch 0038\n"
    '    if provider_name in {"zai", "glm"}:\n'
    '        return BillingRoute(provider="zai", model=model.split("/")[-1], base_url=base_url or "", billing_mode="official_docs_snapshot")\n'
    "    # Vertex AI hosts the same Gemini models as Google AI Studio; price them\n"
)

# ---------------------------------------------------------------------------
# Fix 2: _OFFICIAL_DOCS_PRICING — add glm-5.2 entry
# ---------------------------------------------------------------------------
# Anchor: closing of the dict (, followed by the alias block.
# Must be unique — the closing } plus the GPT-5.6 comment that follows.

_PRICES_ANCHOR = (
    "    ),\n"
    "}\n"
    "\n"
    "# GPT-5.6 \"-pro\" high-effort variants bill at the same per-token rates as\n"
)

_PRICES_MARKER = "glm-5.2"

_PRICES_INSERTION = """\
    # ── Z.ai (GLM) ───────────────────────────────────────────────────────
    # Official API pricing from https://docs.z.ai/guides/overview/pricing
    # (2026-07). All GLM-5.x text models included; older 4.x models added
    # on-demand when a session actually uses them.
    (
        "zai",
        "glm-5.2",
    ): PricingEntry(
        input_cost_per_million=Decimal("1.40"),
        output_cost_per_million=Decimal("4.40"),
        cache_read_cost_per_million=Decimal("0.26"),
        source="official_docs_snapshot",
        source_url="https://docs.z.ai/guides/overview/pricing",
        pricing_version="zai-pricing-2026-07",
    ),
    (
        "zai",
        "glm-5.1",
    ): PricingEntry(
        input_cost_per_million=Decimal("1.40"),
        output_cost_per_million=Decimal("4.40"),
        cache_read_cost_per_million=Decimal("0.26"),
        source="official_docs_snapshot",
        source_url="https://docs.z.ai/guides/overview/pricing",
        pricing_version="zai-pricing-2026-07",
    ),
    (
        "zai",
        "glm-5",
    ): PricingEntry(
        input_cost_per_million=Decimal("1.00"),
        output_cost_per_million=Decimal("3.20"),
        cache_read_cost_per_million=Decimal("0.20"),
        source="official_docs_snapshot",
        source_url="https://docs.z.ai/guides/overview/pricing",
        pricing_version="zai-pricing-2026-07",
    ),
""" + "\n"

# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _patch_route() -> None:
    spec = importlib.util.find_spec("agent.usage_pricing")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/usage_pricing.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(route): already applied to {path} -- no-op")
        return

    count = src.count(_ROUTE_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(route): expected 1 minimax/vertex anchor in {path}, "
            f"found {count} (upstream changed resolve_billing_route — re-verify)"
        )
    src = src.replace(_ROUTE_ANCHOR, _ROUTE_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(route): zai/glm branch added to resolve_billing_route in {path}")


def _patch_prices() -> None:
    spec = importlib.util.find_spec("agent.usage_pricing")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/usage_pricing.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _PRICES_MARKER in src:
        print(f"patch(prices): already applied to {path} -- no-op")
        return

    count = src.count(_PRICES_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(prices): expected 1 closing-dict anchor in {path}, "
            f"found {count} (upstream changed pricing dict layout — re-verify)"
        )
    src = src.replace(_PRICES_ANCHOR, _PRICES_INSERTION + _PRICES_ANCHOR, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(prices): glm-5.2 pricing injected into {path}")


def main() -> int:
    _patch_route()
    _patch_prices()
    return 0


if __name__ == "__main__":
    sys.exit(main())
