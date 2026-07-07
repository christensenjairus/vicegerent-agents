#!/usr/bin/env python3
"""Vicegerent patch for tools/mcp_tool.py: auto-retry a parked MCP server.

Problem
-------
When an MCP server's connection drops, ``MCPServerConnection.run()`` retries
with exponential backoff up to ``_MAX_RECONNECT_RETRIES`` (5) times, capped
at ``_MAX_BACKOFF_SECONDS`` (60s) between attempts. Once that budget is
exhausted the server "parks": it deregisters its tools and blocks in
``_wait_for_reconnect_or_shutdown()`` FOREVER, waiting for something else to
explicitly set ``_reconnect_event`` -- a tool call bumping the circuit
breaker (which only fires if the model tries to call one of that server's
now-deregistered tools), OAuth recovery, or a manual ``/mcp`` refresh.

In this deployment the sole MCP connection is "vmcp" (ToolHive's aggregator
fronting kubernetes/gitlab/github/tavily/firecrawl/notion/linear/jira/
grafana/alertmanager/pagerduty -- see 0009's patch docstring). If vMCP goes
down for longer than ~2 minutes (5 retries x up to 60s backoff), Hermes
parks it and never notices vMCP came back on its own -- the operator has to
restart Hermes to get a fresh ``run()`` loop. There is no periodic
self-probe once parked; a downed backend with no active tool traffic can
stay dead for the rest of the process's life.

Fix
---
``_wait_for_reconnect_or_shutdown()``'s ``asyncio.wait(...)`` call currently
has no ``timeout=``, so it blocks until ``_shutdown_event`` or
``_reconnect_event`` fires with no other exit. Add a timeout of
``parked_retry_interval`` (server config, default
``_DEFAULT_PARKED_RETRY_INTERVAL`` = 30s, floored at
``_MIN_PARKED_RETRY_INTERVAL`` = 10s) so a timed-out wait falls through to a
self-triggered reconnect attempt exactly like a real ``_reconnect_event`` --
same code path ``run()`` already uses for the breaker/OAuth/manual-refresh
wakeups, just periodic instead of externally triggered. This means a parked
server now retries forever on a fixed cadence with no operator
intervention, while still instantly waking early if something DOES fire
``_reconnect_event`` (no need to wait out the full interval for those
paths).

Both new constants and the timeout value are configurable per server via
``parked_retry_interval`` in ``mcp_servers.<name>`` (mirrors the existing
``keepalive_interval`` pattern in the same file), but ship a sane default
so no config change is required to get the retry-forever behavior this
patch adds.

Verified end-to-end against a real copy of the installed module (apply,
diff, py_compile, then drive ``_wait_for_reconnect_or_shutdown`` synchronously
with a fake ``_config``/events to confirm the timeout path returns
"reconnect" without either event being set) before being committed.

Fail-loud by design: if either anchor is absent or appears more than once
(i.e. upstream refactored this path), the patch raises and the image build
fails, signalling a re-verify.
"""
import importlib.util
import sys

# --- Constants: new parked-retry knobs, injected next to the existing
# reconnect/backoff constants so they're easy to find together. ---------

CONST_ANCHOR = "_MAX_BACKOFF_SECONDS = 60\n"

CONST_BLOCK = (
    "_MAX_BACKOFF_SECONDS = 60\n"
    "\n"
    "# Vicegerent patch: once a server exhausts _MAX_RECONNECT_RETRIES it\n"
    "# \"parks\" -- previously this meant it sat idle forever until something\n"
    "# else (a tool call bumping the circuit breaker, OAuth recovery, or a\n"
    "# manual /mcp refresh) explicitly asked it to come back. That left a\n"
    "# downed server dead for the rest of the process's life if nothing\n"
    "# happened to touch it -- e.g. a long-lived gateway session where the\n"
    "# agent never calls that server's tools again after the outage starts.\n"
    "# Keep probing on a timer while parked instead: same self-heal behavior\n"
    "# as the pre-park reconnect loop, just at a slower, indefinite cadence so\n"
    "# a permanently-down server doesn't busy-loop. Configurable per server via\n"
    "# ``parked_retry_interval`` (seconds); floored at\n"
    "# _MIN_PARKED_RETRY_INTERVAL so a misconfigured tiny value can't hot-loop.\n"
    "_DEFAULT_PARKED_RETRY_INTERVAL = 30  # seconds between parked auto-reconnect attempts\n"
    "_MIN_PARKED_RETRY_INTERVAL = 10      # clamp floor for configured intervals\n"
)

# --- _wait_for_reconnect_or_shutdown: add a timeout so a parked server
# retries on its own instead of waiting forever for an external nudge. ---

METHOD_ANCHOR = (
    "    async def _wait_for_reconnect_or_shutdown(self) -> str:\n"
    "        \"\"\"Block until a reconnect or shutdown is requested while parked.\n"
    "\n"
    "        Used by :meth:`run` after the reconnect budget is exhausted. The\n"
    "        task stays alive (so ``_reconnect_event`` always has a listener) but\n"
    "        does no work until something explicitly asks it to come back —\n"
    "        the circuit-breaker half-open probe, OAuth recovery, or a manual\n"
    "        ``/mcp`` refresh.\n"
    "\n"
    "        Returns:\n"
    "            ``\"shutdown\"`` if the server should exit the run loop entirely,\n"
    "            ``\"reconnect\"`` if it should rebuild the transport. The reconnect\n"
    "            event is cleared before returning so the next park cycle starts\n"
    "            from a fresh signal. Shutdown takes precedence.\n"
    "        \"\"\"\n"
    "        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())\n"
    "        reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())\n"
    "        try:\n"
    "            await asyncio.wait(\n"
    "                {shutdown_task, reconnect_task},\n"
    "                return_when=asyncio.FIRST_COMPLETED,\n"
    "            )\n"
    "        finally:\n"
    "            for t in (shutdown_task, reconnect_task):\n"
    "                if not t.done():\n"
    "                    t.cancel()\n"
    "                    try:\n"
    "                        await t\n"
    "                    except (asyncio.CancelledError, Exception):\n"
    "                        pass\n"
    "        if self._shutdown_event.is_set():\n"
    "            return \"shutdown\"\n"
    "        self._reconnect_event.clear()\n"
    "        return \"reconnect\"\n"
)

METHOD_REPLACEMENT = (
    "    async def _wait_for_reconnect_or_shutdown(self) -> str:\n"
    "        \"\"\"Block until a reconnect or shutdown is requested while parked,\n"
    "        or until ``parked_retry_interval`` elapses (Vicegerent patch).\n"
    "\n"
    "        Used by :meth:`run` after the reconnect budget is exhausted. The\n"
    "        task stays alive (so ``_reconnect_event`` always has a listener),\n"
    "        doing no active work while parked, but no longer waits forever for\n"
    "        an external nudge -- the circuit-breaker half-open probe, OAuth\n"
    "        recovery, and manual ``/mcp`` refresh all still wake it instantly,\n"
    "        but a timeout of ``parked_retry_interval`` (config default\n"
    "        ``_DEFAULT_PARKED_RETRY_INTERVAL``, floored at\n"
    "        ``_MIN_PARKED_RETRY_INTERVAL``) also triggers a self-initiated\n"
    "        reconnect attempt on a fixed cadence, so a downed server that\n"
    "        nothing else happens to probe still self-heals once the upstream\n"
    "        comes back, indefinitely, with no operator intervention.\n"
    "\n"
    "        Returns:\n"
    "            ``\"shutdown\"`` if the server should exit the run loop entirely,\n"
    "            ``\"reconnect\"`` if it should rebuild the transport (whether\n"
    "            from an explicit signal or the periodic timeout). The reconnect\n"
    "            event is cleared before returning so the next park cycle starts\n"
    "            from a fresh signal. Shutdown takes precedence.\n"
    "        \"\"\"\n"
    "        parked_retry_interval = max(\n"
    "            _MIN_PARKED_RETRY_INTERVAL,\n"
    "            float((self._config or {}).get(\n"
    "                \"parked_retry_interval\", _DEFAULT_PARKED_RETRY_INTERVAL\n"
    "            )),\n"
    "        )\n"
    "        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())\n"
    "        reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())\n"
    "        try:\n"
    "            await asyncio.wait(\n"
    "                {shutdown_task, reconnect_task},\n"
    "                timeout=parked_retry_interval,\n"
    "                return_when=asyncio.FIRST_COMPLETED,\n"
    "            )\n"
    "        finally:\n"
    "            for t in (shutdown_task, reconnect_task):\n"
    "                if not t.done():\n"
    "                    t.cancel()\n"
    "                    try:\n"
    "                        await t\n"
    "                    except (asyncio.CancelledError, Exception):\n"
    "                        pass\n"
    "        if self._shutdown_event.is_set():\n"
    "            return \"shutdown\"\n"
    "        # Either an explicit reconnect was requested, or the timeout\n"
    "        # elapsed with neither event set -- both cases retry the\n"
    "        # transport. Clearing here is a no-op in the timeout case (the\n"
    "        # event was never set) and correct in the explicit case (starts\n"
    "        # the next park cycle with a fresh signal).\n"
    "        self._reconnect_event.clear()\n"
    "        return \"reconnect\"\n"
)

APPLIED_MARKER = "self-initiated\n        reconnect attempt on a fixed cadence"


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

    const_count = src.count(CONST_ANCHOR)
    if const_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 CONST_ANCHOR in {path}, found "
            f"{const_count} (upstream drifted -- re-verify the reconnect/backoff "
            "constants block)"
        )
    src = src.replace(CONST_ANCHOR, CONST_BLOCK, 1)

    method_count = src.count(METHOD_ANCHOR)
    if method_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 METHOD_ANCHOR in {path}, found "
            f"{method_count} (upstream drifted -- re-verify "
            "_wait_for_reconnect_or_shutdown, the fix may need to move or upstream "
            "may have already added a parked-retry timeout of its own)"
        )
    src = src.replace(METHOD_ANCHOR, METHOD_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied MCP parked-auto-retry fix to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
