#!/usr/bin/env python3
"""Carry a Hermes MCP shutdown-timeout patch in the vicegerent image.

This is a local image patch for the egress-locked sandbox. It shortens teardown
for remote HTTP/SSE MCP sessions while preserving the longer grace for stdio MCP
servers that may own local child processes. Remove this layer once upstream
Hermes has equivalent behavior.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE = "tools.mcp_tool"
APPLIED_MARKER = "_DEFAULT_HTTP_SHUTDOWN_TIMEOUT = 3.0"

CONSTANTS_ANCHOR = """_DEFAULT_TOOL_TIMEOUT = 300      # seconds for tool calls
_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # retries for the very first connection attempt
_MAX_BACKOFF_SECONDS = 60
"""
CONSTANTS_REPLACEMENT = """_DEFAULT_TOOL_TIMEOUT = 300      # seconds for tool calls
_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_DEFAULT_STDIO_SHUTDOWN_TIMEOUT = 10.0  # seconds for stdio server teardown
_DEFAULT_HTTP_SHUTDOWN_TIMEOUT = 3.0    # seconds for remote HTTP session teardown
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # retries for the very first connection attempt
_MAX_BACKOFF_SECONDS = 60
"""

HELPER_ANCHOR = """def _safe_numeric(value, default, coerce=int, minimum=1):
    \"\"\"Coerce a config value to a numeric type, returning *default* on failure.

    Handles string values from YAML (e.g. ``\"10\"`` instead of ``10``),
    non-finite floats, and values below *minimum*.
    \"\"\"
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


class SamplingHandler:
"""
HELPER_REPLACEMENT = """def _safe_numeric(value, default, coerce=int, minimum=1):
    \"\"\"Coerce a config value to a numeric type, returning *default* on failure.

    Handles string values from YAML (e.g. ``\"10\"`` instead of ``10``),
    non-finite floats, and values below *minimum*.
    \"\"\"
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


def _shutdown_timeout_for_config(config: dict, *, is_http: bool) -> float:
    \"\"\"Return the bounded graceful-shutdown timeout for one MCP server.

    Stdio MCP servers may own child processes that need a little grace to exit,
    but remote HTTP/SSE sessions should not hold process shutdown for the same
    long window: they have no local process tree to preserve, and failed remote
    session termination is only advisory cleanup. ``shutdown_timeout`` remains
    configurable per server for installations that need a different bound.
    \"\"\"
    default = (
        _DEFAULT_HTTP_SHUTDOWN_TIMEOUT
        if is_http
        else _DEFAULT_STDIO_SHUTDOWN_TIMEOUT
    )
    try:
        result = float(config.get("shutdown_timeout", default))
        if not math.isfinite(result):
            return default
        return max(result, 0.5)
    except (TypeError, ValueError, OverflowError):
        return default


class SamplingHandler:
"""

SHUTDOWN_ANCHOR = """        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    \"MCP server '%s' shutdown timed out, cancelling task\",
                    self.name,
                )
                self._task.cancel()
"""
SHUTDOWN_REPLACEMENT = """        if self._task and not self._task.done():
            shutdown_timeout = _shutdown_timeout_for_config(
                self._config,
                is_http=self._is_http(),
            )
            try:
                await asyncio.wait_for(self._task, timeout=shutdown_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    \"MCP server '%s' shutdown timed out after %.1fs, cancelling task\",
                    self.name,
                    shutdown_timeout,
                )
                self._task.cancel()
"""

EXAMPLE_ANCHOR = """        timeout: 120         # per-tool-call timeout in seconds (default: 300)
        connect_timeout: 60  # initial connection timeout (default: 60)
"""
EXAMPLE_REPLACEMENT = """        timeout: 120         # per-tool-call timeout in seconds (default: 300)
        connect_timeout: 60  # initial connection timeout (default: 60)
        shutdown_timeout: 10 # graceful shutdown timeout (stdio default: 10s,
                             # HTTP/SSE default: 3s)
"""

REPLACEMENTS = [
    (CONSTANTS_ANCHOR, CONSTANTS_REPLACEMENT),
    (HELPER_ANCHOR, HELPER_REPLACEMENT),
    (SHUTDOWN_ANCHOR, SHUTDOWN_REPLACEMENT),
    (EXAMPLE_ANCHOR, EXAMPLE_REPLACEMENT),
]


def main() -> None:
    spec = importlib.util.find_spec(MODULE)
    if spec is None or spec.origin is None:
        raise SystemExit(f"Could not locate {MODULE}")

    path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")

    if APPLIED_MARKER in source:
        print(f"mcp shutdown-timeout patch already applied in {path}")
        return

    patched = source
    for anchor, replacement in REPLACEMENTS:
        count = patched.count(anchor)
        if count != 1:
            raise SystemExit(
                f"Patch anchor mismatch for {path}: expected 1 occurrence, found {count}. "
                "Upstream changed; re-verify the carried patch."
            )
        patched = patched.replace(anchor, replacement)

    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8")
    print(f"mcp shutdown-timeout patch applied to {path}")


if __name__ == "__main__":
    main()
