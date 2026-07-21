#!/usr/bin/env python3
"""Vicegerent patches for the agentburn package.

Two fixes bundled here because they both patch agentburn internals and should
be dropped together when agentburn upstreams support for them.

1. HERMES_HOME support (adapters/hermes.py)
   default_db_path() hardcodes ~/.hermes/state.db. In the vicegerent sandbox
   Hermes stores state.db at $HERMES_HOME/state.db (/opt/data/state.db), so
   the agentburn MCP server can't find the DB without this fix.

2. Missing model prices (prices.py)
   agentburn's prices.py is a static snapshot. Models not in the snapshot
   produce cost_basis='unknown'. This injects the models we run in vicegerent.
   Prices are USD per 1M tokens from official rate cards (2026-06).
   Verify before updating:
     https://www.anthropic.com/pricing
     https://platform.openai.com/docs/pricing

Remove both fixes once agentburn upstreams HERMES_HOME support and adds these
models to its own snapshot (or supports a user-supplied price overlay).
"""
import importlib.util
import sys


# ---------------------------------------------------------------------------
# Fix 1: HERMES_HOME support in agentburn.adapters.hermes
# ---------------------------------------------------------------------------

_HOME_ANCHOR = (
    'def default_db_path() -> str:\n'
    '    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'
)

_HOME_REPLACEMENT = (
    'def default_db_path() -> str:\n'
    '    _home = os.environ.get("HERMES_HOME", "").strip()\n'
    '    if _home:\n'
    '        return os.path.join(_home, "state.db")\n'
    '    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'
)

_HOME_MARKER = 'HERMES_HOME", "").strip()'


def _patch_hermes_home() -> None:
    spec = importlib.util.find_spec("agentburn.adapters.hermes")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn.adapters.hermes module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _HOME_MARKER in src:
        print(f"patch(hermes-home): already applied to {path} -- no-op")
        return

    count = src.count(_HOME_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(hermes-home): expected 1 anchor in {path}, found {count} "
            "(agentburn upstream changed adapters/hermes.py -- re-verify)"
        )

    src = src.replace(_HOME_ANCHOR, _HOME_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(hermes-home): HERMES_HOME support added to {path}")


# ---------------------------------------------------------------------------
# Fix 2: missing model prices in agentburn.prices
# ---------------------------------------------------------------------------

# fmt: off
_EXTRA_PRICES = {
    # Anthropic — https://www.anthropic.com/pricing (2026-06)
    "anthropic/claude-haiku-4-5":           (1.00,  5.00),
    "anthropic/claude-haiku-4-5-20251001":  (1.00,  5.00),
    "anthropic/claude-sonnet-4-5":          (3.00, 15.00),
    "anthropic/claude-sonnet-4-5-20250929": (3.00, 15.00),
    "anthropic/claude-opus-4-5":            (5.00, 25.00),
    "anthropic/claude-opus-4-5-20251101":   (5.00, 25.00),
    "anthropic/claude-opus-4-7":            (5.00, 25.00),
    "anthropic/claude-opus-4-8":            (5.00, 25.00),
    "anthropic/claude-fable-5":             (10.00, 50.00),
    # OpenAI — https://platform.openai.com/docs/pricing (2026-06)
    # Short-context price (<272K tokens); long-context roughly doubles both
    # input and output. gpt-5.6-sol/gpt-5.5 have no separate long-context
    # cache-writes rate; gpt-5.5-pro/gpt-5.4-pro have no cached-input tier.
    "openai/gpt-4.1":                       (2.00,  8.00),
    "openai/gpt-4o-mini":                   (0.15,  0.60),
    "openai/gpt-5.6-sol":                   (5.00, 30.00),
    "openai/gpt-5.6-terra":                 (2.50, 15.00),
    "openai/gpt-5.6-luna":                  (1.00,  6.00),
    "openai/gpt-5.5":                       (5.00, 30.00),
    "openai/gpt-5.5-pro":                   (30.00, 180.00),
}
# fmt: on

# Anchor: closing brace of PRICES dict, followed by the CHEAP_REFERENCE line.
_PRICES_ANCHOR = "}\n\nCHEAP_REFERENCE"
_PRICES_MARKER = "anthropic/claude-haiku-4-5"


def _patch_prices() -> None:
    spec = importlib.util.find_spec("agentburn.prices")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn.prices module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _PRICES_MARKER in src:
        print(f"patch(prices): already applied to {path} -- no-op")
        return

    count = src.count(_PRICES_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(prices): expected 1 anchor in {path}, found {count} "
            "(agentburn upstream changed prices.py layout -- re-verify)"
        )

    extra_lines = "\n".join(
        f'    "{slug}": {prices!r},'
        for slug, prices in sorted(_EXTRA_PRICES.items())
    )
    insertion = f"    # vicegerent: models added by patch 0004\n{extra_lines}\n"
    src = src.replace(_PRICES_ANCHOR, insertion + _PRICES_ANCHOR, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(prices): injected {len(_EXTRA_PRICES)} custom prices into {path}")


# ---------------------------------------------------------------------------

def main() -> int:
    _patch_hermes_home()
    _patch_prices()
    return 0


if __name__ == "__main__":
    sys.exit(main())
