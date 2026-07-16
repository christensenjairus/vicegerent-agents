#!/usr/bin/env python3
"""Vicegerent patch: unwrap the swallowed sub-exception in tools/mcp_tool.py's
'connection lost' reconnect-loop warning.

Context / investigation
------------------------
/opt/data/logs/errors.log shows 564 occurrences since 2026-07-04 of:

    WARNING tools.mcp_tool: MCP server 'vmcp' connection lost (attempt N/5),
    reconnecting in Xs: unhandled errors in a TaskGroup (1 sub-exception)

...in tight exponential-backoff bursts (1s/2s/4s/8s/16s), roughly daily,
sometimes exhausting all 5 reconnect attempts and parking the server (see
patch 0022's _PARKED_RETRY_INTERVAL). The log line never reveals what
actually failed.

Root cause of the *opacity* (not of the underlying network failure, which
this patch does not attempt to fix): ``MCPServerConnection.run()``'s
reconnect loop (tools/mcp_tool.py) has:

    while True:
        try:
            ...
            lifecycle_reason = await self._run_http(config)  # or _run_stdio
            ...
        except asyncio.CancelledError:
            ...
            raise
        except Exception as exc:
            ...
            logger.warning(
                "MCP server '%s' connection lost (attempt %d/%d), "
                "reconnecting in %.0fs: %s",
                self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,
                backoff, exc,
            )

``_run_http``/``_run_stdio`` open an anyio ``TaskGroup`` (via the MCP SDK's
streamable-HTTP/stdio client context managers) to run the read/write/session
loops concurrently. When one of those inner tasks raises -- e.g. an
``httpx.ReadError``/``httpx.RemoteProtocolError``/``ConnectError`` on a
dropped streamable-HTTP connection, or an ``anyio.EndOfStream`` on a closed
stdio pipe -- anyio's TaskGroup cancels its siblings and re-raises as a
``BaseExceptionGroup``/``ExceptionGroup`` wrapping exactly that one
sub-exception ("(1 sub-exception)"). The bare ``except Exception as exc:``
above catches the (Base)ExceptionGroup itself (it IS an Exception subclass
in 3.11+), and `%s` formatting on it produces only anyio/CPython's generic
group repr -- "unhandled errors in a TaskGroup (1 sub-exception)" -- never
descending into ``exc.exceptions`` to show the real ``httpx``/``anyio``
error that's actually the story here.

Fix
---
Purely a logging/diagnostics change -- no reconnect timing, backoff, retry
budget, or control flow is touched. Add a small helper,
``_describe_exception_detail(exc)``, placed near the other exception-
classification helpers (``_is_auth_error`` etc.), that:

  - If ``exc`` is a ``BaseExceptionGroup``/``ExceptionGroup``, recursively
    walks ``exc.exceptions`` (groups can nest) and returns a
    "TypeName: message" string for each leaf sub-exception, joined by "; ".
  - Otherwise returns "TypeName: message" for the exception itself.

The 'connection lost' log line's existing ``%s`` interpolation (``exc``,
which still prints the outer ExceptionGroup summary) is left completely
intact; a new final ``%s`` argument is appended showing the unwrapped
detail, e.g.:

    MCP server 'vmcp' connection lost (attempt 2/5), reconnecting in 4s:
    unhandled errors in a TaskGroup (1 sub-exception) [ReadError: ...]

so the next occurrence's log line is directly actionable instead of opaque.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this log line or the reconnect loop), the patch raises
and the image build fails, signalling a re-verify.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0024"

HELPER_ANCHOR = "def _is_auth_error(exc: BaseException) -> bool:\n"

HELPER_BLOCK = (
    "def _describe_exception_detail(exc: BaseException) -> str:\n"
    "    \"\"\"Unwrap (Base)ExceptionGroup into a readable sub-exception summary.\n"
    "\n"
    "    Vicegerent patch 0024: anyio TaskGroups (used by the MCP SDK's\n"
    "    streamable-HTTP/stdio transports) wrap any single inner-task failure\n"
    "    in a BaseExceptionGroup/ExceptionGroup whose default str() is just\n"
    "    \"unhandled errors in a TaskGroup (N sub-exception(s))\" -- the actual\n"
    "    httpx/anyio error is invisible in logs unless callers descend into\n"
    "    ``exceptions``. Recurses because groups can nest.\n"
    "    \"\"\"\n"
    "    try:\n"
    "        sub_exceptions = getattr(exc, \"exceptions\", None)\n"
    "    except Exception:\n"
    "        sub_exceptions = None\n"
    "    if sub_exceptions:\n"
    "        parts = [_describe_exception_detail(sub) for sub in sub_exceptions]\n"
    "        return \"; \".join(parts)\n"
    "    return f\"{type(exc).__name__}: {exc}\"\n"
    "\n"
    "\n"
)

LOG_ANCHOR = (
    "                logger.warning(\n"
    "                    \"MCP server '%s' connection lost (attempt %d/%d), \"\n"
    "                    \"reconnecting in %.0fs: %s\",\n"
    "                    self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,\n"
    "                    backoff, exc,\n"
    "                )\n"
)

LOG_REPLACEMENT = (
    "                # Vicegerent patch 0024: anyio wraps the real network\n"
    "                # failure in a bare ExceptionGroup here, whose default %s\n"
    "                # is just \"unhandled errors in a TaskGroup (1 sub-exception)\"\n"
    "                # -- unwrap it so the actual httpx/anyio error is visible.\n"
    "                logger.warning(\n"
    "                    \"MCP server '%s' connection lost (attempt %d/%d), \"\n"
    "                    \"reconnecting in %.0fs: %s [%s]\",\n"
    "                    self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,\n"
    "                    backoff, exc, _describe_exception_detail(exc),\n"
    "                )\n"
)


def main() -> int:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/mcp_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    helper_count = src.count(HELPER_ANCHOR)
    if helper_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 HELPER_ANCHOR (_is_auth_error def) in "
            f"{path}, found {helper_count} (upstream drifted — re-verify)"
        )

    log_count = src.count(LOG_ANCHOR)
    if log_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 LOG_ANCHOR ('connection lost' warning) "
            f"in {path}, found {log_count} (upstream drifted — re-verify)"
        )

    marked_helper_block = HELPER_BLOCK.replace(
        '"""Unwrap',
        f'"""{APPLIED_MARKER}: unwrap',
        1,
    )
    src = src.replace(HELPER_ANCHOR, marked_helper_block + HELPER_ANCHOR, 1)
    src = src.replace(LOG_ANCHOR, LOG_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: connection-lost diagnostics unwrapping added to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
