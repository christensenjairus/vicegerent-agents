#!/usr/bin/env python3
"""Vicegerent patch: add Anthropic model prices missing from the agentburn
embedded snapshot so sessions that use these models report a real cost instead
of cost_basis='unknown'.

agentburn's prices.py is a static snapshot updated with each release. Models
not in the snapshot silently produce None from prices.lookup() — they show up
in reports as 'unknown' cost with no dollar figure. This patch injects the
models we actually run in the vicegerent sandbox.

Prices are USD per 1M tokens (prompt, completion) from Anthropic's published
rate card. Verify against https://www.anthropic.com/pricing before updating.

Remove this patch once agentburn supports a user-supplied price overlay
(e.g. ~/.agentburn/prices.json) or adds these models to its own snapshot.
"""
import importlib.util
import sys

# fmt: off
EXTRA_PRICES = {
    # Anthropic models not in the agentburn 0.11.0 snapshot.
    # Rates: https://docs.anthropic.com/en/docs/about-claude/pricing (as of 2026-06)
    "anthropic/claude-haiku-4-5":              (1.00,  5.00),
    "anthropic/claude-haiku-4-5-20251001":     (1.00,  5.00),
    "anthropic/claude-sonnet-4-5":             (3.00, 15.00),
    "anthropic/claude-sonnet-4-5-20250929":    (3.00, 15.00),
    "anthropic/claude-opus-4-5":               (5.00, 25.00),
    "anthropic/claude-opus-4-5-20251101":      (5.00, 25.00),
    "anthropic/claude-opus-4-7":               (5.00, 25.00),
    "anthropic/claude-opus-4-8":               (5.00, 25.00),
    "anthropic/claude-fable-5":                (10.00, 50.00),
    # OpenAI models not in the agentburn 0.11.0 snapshot.
    # Rates: https://developers.openai.com/api/docs/pricing (as of 2026-06)
    # gpt-5.5 short-context price (<272K tokens); long-context is $10/$45.
    "openai/gpt-4.1":                          (2.00,  8.00),
    "openai/gpt-4o-mini":                      (0.15,  0.60),
    "openai/gpt-5.5":                          (5.00, 30.00),
}
# fmt: on

# Anchor: the closing brace of the PRICES dict, followed by the CHEAP_REFERENCE line.
# We insert our entries just before the closing brace so they land inside the dict.
ANCHOR = "}\n\nCHEAP_REFERENCE"
APPLIED_MARKER = "anthropic/claude-haiku-4-5"


def main() -> int:
    spec = importlib.util.find_spec("agentburn.prices")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn.prices module")
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
            "(agentburn upstream changed prices.py layout — re-verify)"
        )

    extra_lines = "\n".join(
        f'    "{slug}": {prices!r},'
        for slug, prices in sorted(EXTRA_PRICES.items())
    )
    insertion = f"    # vicegerent: models added by patch 0004\n{extra_lines}\n"
    src = src.replace(ANCHOR, insertion + ANCHOR, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: injected {len(EXTRA_PRICES)} custom prices into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
