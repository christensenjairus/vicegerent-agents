#!/usr/bin/env python3
"""Raise tools/mcp_tool.py's MCP circuit-breaker strike threshold 3 -> 15.

Upstream opens the per-server (here per-backend, see 0009) circuit breaker
after _CIRCUIT_BREAKER_THRESHOLD (default 3) consecutive real
transport/auth failures, then short-circuits every tool on that key for
_CIRCUIT_BREAKER_COOLDOWN_SEC (60s). Combined with 0009 — which already
stops business/isError responses from counting and scopes the breaker per
vMCP backend — three genuine transport blips are too twitchy for this
deployment: the sole MCP path is the host vMCP aggregator over ghostunnel
mTLS, where a backend can emit a burst of real timeouts under load before
recovering, and tripping at 3 needlessly parks that backend for a full
minute. Fifteen strikes keeps the anti-burn-loop protection (still far
below the #10447 runaway) while tolerating transient flakiness.

Only the numeric constant changes; the cooldown, state machine, and both
0009 fixes are untouched. Ordered after 0009 in the Dockerfile, but anchor-
independent of it (0009 does not touch this line).

Fail-loud by design: if the anchor is absent or appears more than once
(upstream changed the default or refactored this block), the patch raises
and the image build fails, signalling a re-verify. Idempotent: a re-run
after a successful apply is a no-op.

Remove once upstream exposes the threshold as a per-server config option
(or if the default itself changes to something we're happy with).
"""
import importlib.util
import sys

ANCHOR = "_CIRCUIT_BREAKER_THRESHOLD = 3"
REPLACEMENT = "_CIRCUIT_BREAKER_THRESHOLD = 15"


def main() -> int:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/mcp_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if REPLACEMENT in src:
        print(f"patch: circuit-breaker threshold already 15 in {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 '{ANCHOR}' in {path}, found {count} "
            "(upstream changed the circuit-breaker default or refactored the "
            "constant — re-verify the intended strike threshold)"
        )
    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: raised MCP circuit-breaker threshold to 15 in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
