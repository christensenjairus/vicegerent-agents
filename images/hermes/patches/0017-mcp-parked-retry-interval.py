#!/usr/bin/env python3
"""Vicegerent patch for tools/mcp_tool.py: shorten the parked-retry interval.

Background
----------
When an MCP server exhausts its reconnect budget (``_MAX_RECONNECT_RETRIES``
exponential-backoff attempts) it "parks": it deregisters its tools and, since
those tools are out of the registry, nothing can reach the circuit-breaker
half-open probe or ``_signal_reconnect``. As of Hermes v2026.7.7 upstream
handles this itself -- ``_wait_for_reconnect_or_shutdown(timeout=...)`` wakes on
a fixed cadence (``_PARKED_RETRY_INTERVAL``) and self-probes one revival attempt
per wake, so a downed backend that comes back on its own is revived without an
operator restart. (Earlier Vicegerent builds carried a larger patch that added
this whole mechanism; upstream has since absorbed it, so all that remains is the
cadence.)

Fix
---
Upstream defaults ``_PARKED_RETRY_INTERVAL`` to 300s (5 minutes). In this
deployment the sole MCP connection is "vmcp" (ToolHive's aggregator fronting
kubernetes/gitlab/github/tavily/firecrawl/notion/linear/jira/grafana/
alertmanager/pagerduty). A 5-minute blind spot after a vMCP blip is too long for
an interactive agent, so lower the cadence to 15s: a downed vMCP that recovers
is picked up within ~15s instead of ~5min. One probe per wake still means a
still-dead server just parks again for another interval -- no busy-loop.

Fail-loud by design: if the constant is absent or appears more than once (i.e.
upstream renamed/removed it or changed the default expression), the patch raises
and the image build fails, signalling a re-verify.
"""
import importlib.util
import sys

ANCHOR = "_PARKED_RETRY_INTERVAL = 300     # seconds between parked self-probes\n"
REPLACEMENT = (
    "_PARKED_RETRY_INTERVAL = 15      # seconds between parked self-probes"
    "  # Vicegerent: 15s, not upstream's 300s -- faster vMCP self-heal\n"
)

APPLIED_MARKER = "_PARKED_RETRY_INTERVAL = 15"


def main() -> int:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/mcp_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _PARKED_RETRY_INTERVAL anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify the parked-retry "
            "constant; upstream may have renamed it or changed its default)"
        )
    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: set _PARKED_RETRY_INTERVAL=15 in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
