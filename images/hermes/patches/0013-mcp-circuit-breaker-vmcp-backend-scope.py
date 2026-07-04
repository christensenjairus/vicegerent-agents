#!/usr/bin/env python3
"""Vicegerent patch: limit MCP circuit breaker blast radius in vMCP aggregator.

Context
-------
tools/mcp_tool.py implements a circuit breaker per MCP server: after
_CIRCUIT_BREAKER_THRESHOLD (3) consecutive failures it opens and blocks
ALL tools on that server for _CIRCUIT_BREAKER_COOLDOWN_SEC (60s).

In this deployment, ToolHive's vMCP aggregates ~11 real backends
(kubernetes, gitlab, github, tavily, firecrawl, notion, linear, jira,
grafana, alertmanager, pagerduty) behind ONE Hermes-visible MCP server
connection ("vmcp") using the vMCP "optimizer" mode. The optimizer exposes
two meta-tools: find_tool (search) and call_tool (invoke by name, wrapping
the real backend tool call as {tool_name, parameters}).

Because Hermes's breaker keys strictly on the MCP server_name (which is
just "vmcp" for this connection), a real transport/auth exception from ONE
misbehaving backend (e.g. alertmanager timing out 3x) trips the shared
breaker and blocks find_tool AND every call_tool invocation for all 11
backends, not just the flaky one, for the full 60s cooldown.

Patch 0009 already fixed the "business error" (isError=true relayed as a
JSON error key) false-positive branch on the success path. This patch
targets the remaining "genuine transport/auth exception" branch in
_make_tool_handler's _handler (tools/mcp_tool.py) and narrows its blast
radius for the vMCP-optimizer case specifically, by keying the breaker
per wrapped backend instead of per server_name when the call is to
call_tool.

Verified against the real v0.18.0 tools/mcp_tool.py source (this patch's
predecessor, opened against v0.17.0 conventions, had drifted and no longer
matched: it invented a `request_args` variable that doesn't exist in this
file, and anchored on exception-handling text that isn't present verbatim
in the current source). The real handler signature is
`def _handler(args: dict, **kwargs) -> str`, and the exception branch that
calls `_bump_server_error(server_name)` is:

    _bump_server_error(server_name)
    logger.error(
        "MCP tool %s/%s call failed: %s",
        server_name, tool_name, exc,
    )
    return json.dumps({
        "error": _sanitize_error(
            f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
        )
    }, ensure_ascii=False)

`server_name` here is the Hermes-registered MCP connection name (e.g.
"vmcp"); `tool_name` is the Hermes-registered tool name for that connection
(e.g. "call_tool"); the real wrapped backend tool name lives in `args`
(the handler's own parameter — NOT a separate `request_args`), specifically
`args.get("tool_name")` for the vMCP optimizer's call_tool wrapper shape,
e.g. "alertmanager_getAlerts".

The fix: when `tool_name == "call_tool"`, extract the wrapped tool name
from `args.get("tool_name")`, derive a backend prefix (substring before
the first underscore), and if that prefix is a known ToolHive backend,
bump/reset a breaker counter keyed on `f"{server_name}:{backend_prefix}"`
instead of bare `server_name`. find_tool failures (inherently
cross-backend) and any other tool keep bumping the bare server_name
breaker as before. 3 consecutive alertmanager failures now only block
alertmanager's tools (call_tool invocations whose wrapped tool_name starts
with "alertmanager_"), not github/gitlab/etc.

Strictly additive and backwards-compatible: non-vMCP-optimizer 1:1 MCP
connections (tool_name never equals "call_tool") behave identically to
before this patch. If the wrapped tool_name can't be extracted for any
reason, this falls back to the original bare server_name key.

This is a Vicegerent-specific fix, not an upstream issue — it's unique to
the vMCP optimizer aggregation pattern this repo uses.

Implementation note: the module-level ``_VICEGERENT_MCP_BACKEND_NAMES``
constant is injected directly ahead of the patched function body (rather
than relying on a name defined only in this patch script's own namespace,
which does not exist in the target module at runtime) so the reference in
the replacement text actually resolves after the patch is applied. Verified
end-to-end against a real copy of the installed module (apply, diff,
py_compile, then import + exercise both the vmcp/call_tool and the plain
1:1-connection code paths) before being committed here.
"""
import importlib.util
import sys

# Known ToolHive backend prefixes (from host/mcp/toolhive-servers.json servers[].name).
# Injected as a module-level constant ahead of _make_tool_handler so it's in
# scope for the patched exception branch at runtime.
BACKEND_NAMES_BLOCK = (
    "\n"
    "# Known ToolHive backend prefixes (Vicegerent patch 0013: per-backend\n"
    "# circuit breaker scoping for the vMCP optimizer's call_tool wrapper).\n"
    "_VICEGERENT_MCP_BACKEND_NAMES = {\n"
    "    \"kubernetes\",\n"
    "    \"gitlab\",\n"
    "    \"github\",\n"
    "    \"tavily\",\n"
    "    \"firecrawl\",\n"
    "    \"notion\",\n"
    "    \"linear\",\n"
    "    \"jira\",\n"
    "    \"grafana\",\n"
    "    \"alertmanager\",\n"
    "    \"pagerduty\",\n"
    "}\n"
    "\n"
)

# Anchor for injecting the constant: right before the function whose _handler
# contains the exception branch we patch below.
CONST_ANCHOR = "def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):"

# Anchor: the transport/auth exception branch's tail in _make_tool_handler's
# _handler (tools/mcp_tool.py), verified verbatim against the real v0.18.0
# source. This is the only place in the file where _bump_server_error() is
# called immediately before this exact log+return shape.
ANCHOR = (
    "            _bump_server_error(server_name)\n"
    "            logger.error(\n"
    "                \"MCP tool %s/%s call failed: %s\",\n"
    "                server_name, tool_name, exc,\n"
    "            )\n"
    "            return json.dumps({\n"
    "                \"error\": _sanitize_error(\n"
    "                    f\"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}\"\n"
    "                )\n"
    "            }, ensure_ascii=False)"
)

REPLACEMENT = (
    "            # Per-backend circuit breaker scoping for vMCP optimizer\n"
    "            # aggregation (Vicegerent patch 0013). If this call is to\n"
    "            # call_tool (the vMCP meta-tool wrapping a real backend tool),\n"
    "            # extract the backend prefix from the wrapped tool_name and\n"
    "            # bump the per-backend breaker instead of the server-wide one.\n"
    "            # This prevents one flaky backend (e.g. alertmanager) from\n"
    "            # blocking every other backend (gitlab, github, etc.) behind\n"
    "            # the same vmcp connection. For find_tool or any non-vMCP\n"
    "            # connection, use the bare server_name as before.\n"
    "            breaker_key = server_name\n"
    "            if tool_name == \"call_tool\" and isinstance(args, dict):\n"
    "                real_tool_name = args.get(\"tool_name\")\n"
    "                if isinstance(real_tool_name, str) and real_tool_name:\n"
    "                    backend_prefix = real_tool_name.split(\"_\", 1)[0]\n"
    "                    if backend_prefix in _VICEGERENT_MCP_BACKEND_NAMES:\n"
    "                        breaker_key = f\"{server_name}:{backend_prefix}\"\n"
    "            _bump_server_error(breaker_key)\n"
    "            logger.error(\n"
    "                \"MCP tool %s/%s call failed: %s\",\n"
    "                server_name, tool_name, exc,\n"
    "            )\n"
    "            return json.dumps({\n"
    "                \"error\": _sanitize_error(\n"
    "                    f\"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}\"\n"
    "                )\n"
    "            }, ensure_ascii=False)"
)

# Idempotence marker: unique token from the replacement so a re-run is a
# no-op rather than a hard failure (the anchor is gone after the first apply).
APPLIED_MARKER = "Per-backend circuit breaker scoping for vMCP optimizer"


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

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {path}, found {count} "
            "(upstream drifted — re-verify the MCP transport/auth exception "
            "handler in _make_tool_handler, the per-backend scoping may need "
            "to move)"
        )

    const_count = src.count(CONST_ANCHOR)
    if const_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 CONST_ANCHOR in {path}, found "
            f"{const_count} (cannot safely inject _VICEGERENT_MCP_BACKEND_NAMES)"
        )

    src = src.replace(CONST_ANCHOR, BACKEND_NAMES_BLOCK + CONST_ANCHOR)
    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied MCP circuit-breaker per-backend scoping to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
