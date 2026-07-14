#!/usr/bin/env python3
"""Vicegerent patch: shorten tools/mcp_tool.py's parked-MCP-server
self-probe cadence from upstream's default 300s to 60s.

Context
-------
Patch 0017 (this repo, since removed) hand-added a periodic self-probe
while a parked MCP server (reconnect budget of _MAX_RECONNECT_RETRIES
exhausted, tools deregistered) waits for revival -- otherwise no tool
call can ever reach the circuit-breaker half-open probe or
_signal_reconnect once tools are deregistered, so a downed server with
no active tool traffic stayed dead for the rest of the process's life.

Upstream shipped the same fix natively as of v2026.7.2.2 (tracked as
issue #57129 in tools/mcp_tool.py's own comments): a module-level
_PARKED_RETRY_INTERVAL constant and a timeout= parameter on
_wait_for_reconnect_or_shutdown() that self-triggers a reconnect
attempt on that cadence. 0017 was removed on upgrade because it's now
redundant -- its METHOD_ANCHOR no longer matches the refactored
function signature at all, confirming upstream replaced the exact code
shape it patched.

One regression from removing 0017, though: upstream's
_PARKED_RETRY_INTERVAL is a hardcoded 300s (5 min) with NO
per-server config knob (0017's version was configurable per server via
mcp_servers.<name>.parked_retry_interval, defaulting to 30s). In this
deployment the sole MCP connection is "vmcp" (ToolHive's aggregator --
see 0009's patch docstring); a 5-minute worst-case reconnect delay
after vMCP flaps is worse than the ~30s 0017 gave us, and there's no
config surface to tune it back down anymore.

Fix
---
Rather than reintroduce a configurable knob (more surface than this
deployment needs -- there's exactly one MCP server here, not many with
different tolerances), just lower the hardcoded module constant from
300 to 60 seconds. Splits the difference: fast enough that a vMCP
blip self-heals within a minute, slow enough not to busy-loop probing a
genuinely-down backend.

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream changed the constant's value, name, or
comment), the patch raises and the image build fails, signalling a
re-verify.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0022"

ANCHOR = (
    "# While parked (reconnect budget exhausted, tools deregistered) the run task\n"
    "# wakes on this cadence and attempts one revival probe. Without it a parked\n"
    "# server is unrevivable: its tools are out of the registry, so no tool call\n"
    "# can ever reach the circuit-breaker half-open probe or _signal_reconnect.\n"
    "_PARKED_RETRY_INTERVAL = 300     # seconds between parked self-probes\n"
)

REPLACEMENT = (
    "# While parked (reconnect budget exhausted, tools deregistered) the run task\n"
    "# wakes on this cadence and attempts one revival probe. Without it a parked\n"
    "# server is unrevivable: its tools are out of the registry, so no tool call\n"
    "# can ever reach the circuit-breaker half-open probe or _signal_reconnect.\n"
    "# Vicegerent patch 0022: lowered from upstream's default 300s to 60s -- this\n"
    "# deployment has exactly one MCP server (vmcp, ToolHive's aggregator) and\n"
    "# upstream ships no per-server override for this constant, so a flap longer\n"
    "# than a few seconds but shorter than 5 minutes would otherwise sit parked\n"
    "# far longer than necessary. 60s self-heals promptly without busy-looping a\n"
    "# genuinely-down backend. Remove once upstream makes this configurable via\n"
    "# mcp_servers.<name>.parked_retry_interval (drop back to a config-driven\n"
    "# value instead of a hardcoded module constant).\n"
    "_PARKED_RETRY_INTERVAL = 60      # seconds between parked self-probes\n"
)


def _patch_mcp_tool() -> None:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/mcp_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _PARKED_RETRY_INTERVAL anchor in {path}, "
            f"found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: parked MCP server retry interval lowered to 60s in {path}")


def main() -> int:
    _patch_mcp_tool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
