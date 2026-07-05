#!/usr/bin/env python3
"""Vicegerent patches for tools/mcp_tool.py's MCP circuit breaker.

Two fixes bundled here because they patch the same function
(_make_tool_handler's _handler in tools/mcp_tool.py) and the same
mechanism (the per-server circuit breaker: after
_CIRCUIT_BREAKER_THRESHOLD (3) consecutive "failures" it opens and
blocks ALL tools on that server for _CIRCUIT_BREAKER_COOLDOWN_SEC (60s)).
Originally landed as separate patches 0009 and 0013; merged here since
they touch the same file and function and should be read/maintained as
one story about this breaker.

1. Don't trip the breaker on business errors (success-path fix)
   ---------------------------------------------------------------
   The successful-call branch (after result = _call_once()) treated ANY
   top-level "error" key in the parsed JSON as a circuit-breaker-worthy
   failure — including MCP `result.isError = true` business errors (bad
   arguments, upstream API auth/permission errors, 4xx responses relayed
   by the tool, etc). Those indicate the *tool call* failed, not that the
   *MCP server* is unreachable. In this environment we saw a GitLab MCP
   call return 401 (expired token) then 403 (WAF) then a real transport
   timeout, each counted identically toward the same 3-strike breaker,
   tripping it and blocking retries even seconds after the underlying
   issue (token, WAF) was fixed on the GitLab side.

   Upstream tracks this as https://github.com/NousResearch/hermes-agent
   issues #47851 / #11113, with fixes in flight (#47918, #47955) not yet
   released as of 2026-06-30.

   Fix: a business error (`"error"` key in a normally-returned JSON
   payload) now resets the breaker instead of bumping it — the server
   clearly responded, so it is reachable; the model should just retry
   with different arguments/reasoning. The transport/auth exception
   branch (fix 2, below) is untouched by this fix — actual connection
   failures, timeouts, and unrecovered auth errors still bump the
   breaker.

   Remove this fix once upstream Hermes lands #47918/#47955 (or
   equivalent) and stops bumping the breaker on business errors.

2. Limit blast radius in the vMCP aggregator (exception-path fix)
   ---------------------------------------------------------------
   In this deployment, ToolHive's vMCP aggregates ~11 real backends
   (kubernetes, gitlab, github, tavily, firecrawl, notion, linear, jira,
   grafana, alertmanager, pagerduty) behind ONE Hermes-visible MCP
   server connection ("vmcp") using the vMCP "optimizer" mode. The
   optimizer exposes two meta-tools: find_tool (search) and call_tool
   (invoke by name, wrapping the real backend tool call as
   {tool_name, parameters}).

   Because Hermes's breaker keys strictly on the MCP server_name (just
   "vmcp" for this connection), a real transport/auth exception from ONE
   misbehaving backend (e.g. alertmanager timing out 3x) trips the
   shared breaker and blocks find_tool AND every call_tool invocation
   for all 11 backends, not just the flaky one, for the full 60s
   cooldown.

   Fix: when `tool_name == "call_tool"`, extract the wrapped tool name
   from `args.get("tool_name")`, derive a backend prefix (substring
   before the first underscore), and if that prefix is a known ToolHive
   backend, bump/reset a breaker counter keyed on
   `f"{server_name}:{backend_prefix}"` instead of bare `server_name`.
   find_tool failures (inherently cross-backend) and any other tool keep
   bumping the bare server_name breaker as before. 3 consecutive
   alertmanager failures now only block alertmanager's tools (call_tool
   invocations whose wrapped tool_name starts with "alertmanager_"), not
   github/gitlab/etc.

   Strictly additive and backwards-compatible: non-vMCP-optimizer 1:1
   MCP connections (tool_name never equals "call_tool") behave
   identically to before this fix. If the wrapped tool_name can't be
   extracted for any reason, this falls back to the original bare
   server_name key.

   This is a Vicegerent-specific fix, not an upstream issue — it's
   unique to the vMCP optimizer aggregation pattern this repo uses.

Both fixes verified end-to-end against a real copy of the installed
module (apply, diff, py_compile, then import + exercise both the
vmcp/call_tool and the plain 1:1-connection code paths) before being
committed.

Fail-loud by design: if either anchor is absent or appears more than
once (i.e. upstream refactored this path), the patch raises and the
image build fails, signalling a re-verify.
"""
import importlib.util
import sys

# --- Fix 1: business errors shouldn't bump the breaker -----------------

SUCCESS_ANCHOR = (
    "        try:\n"
    "            result = _call_once()\n"
    "            # Check if the MCP tool itself returned an error\n"
    "            try:\n"
    "                parsed = json.loads(result)\n"
    "                if \"error\" in parsed:\n"
    "                    _bump_server_error(server_name)\n"
    "                else:\n"
    "                    _reset_server_error(server_name)  # success — reset\n"
    "            except (json.JSONDecodeError, TypeError):\n"
    "                _reset_server_error(server_name)  # non-JSON = success\n"
    "            return result\n"
)

SUCCESS_REPLACEMENT = (
    "        try:\n"
    "            result = _call_once()\n"
    "            # Check if the MCP tool itself returned an error. A business\n"
    "            # error (isError=true relayed as a JSON \"error\" key) means the\n"
    "            # server responded and is reachable — reset the breaker rather\n"
    "            # than bump it. Only transport/auth exceptions (fix 2 below)\n"
    "            # indicate real server unreachability.\n"
    "            # Vicegerent patch for hermes-agent #47851 / #11113.\n"
    "            try:\n"
    "                parsed = json.loads(result)\n"
    "                if \"error\" in parsed:\n"
    "                    _reset_server_error(server_name)  # business error — server is up\n"
    "                else:\n"
    "                    _reset_server_error(server_name)  # success — reset\n"
    "            except (json.JSONDecodeError, TypeError):\n"
    "                _reset_server_error(server_name)  # non-JSON = success\n"
    "            return result\n"
)

# --- Fix 2: per-backend breaker scoping for vMCP optimizer --------------

# Known ToolHive backend prefixes (from host/mcp/toolhive-servers.json servers[].name).
# Injected as a module-level constant ahead of _make_tool_handler so it's in
# scope for the patched exception branch at runtime.
BACKEND_NAMES_BLOCK = (
    "\n"
    "# Known ToolHive backend prefixes (Vicegerent patch: per-backend\n"
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
EXCEPTION_ANCHOR = (
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

EXCEPTION_REPLACEMENT = (
    "            # Per-backend circuit breaker scoping for vMCP optimizer\n"
    "            # aggregation (Vicegerent patch). If this call is to\n"
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

# Idempotence markers: unique tokens from each replacement so a re-run is a
# no-op rather than a hard failure (anchors are gone after the first apply).
SUCCESS_APPLIED_MARKER = "Vicegerent patch for hermes-agent #47851 / #11113."
EXCEPTION_APPLIED_MARKER = "Per-backend circuit breaker scoping for vMCP optimizer"


def main() -> int:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/mcp_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    already_success = SUCCESS_APPLIED_MARKER in src
    already_exception = EXCEPTION_APPLIED_MARKER in src
    if already_success and already_exception:
        print(f"patch: already applied (both fixes) to {path} — no-op")
        return 0

    if not already_success:
        count = src.count(SUCCESS_ANCHOR)
        if count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 SUCCESS_ANCHOR in {path}, found {count} "
                "(upstream drifted — re-verify the MCP circuit breaker success path, "
                "the real fix for #47851/#11113 may have landed)"
            )
        src = src.replace(SUCCESS_ANCHOR, SUCCESS_REPLACEMENT)
    else:
        print(f"patch: business-error fix already applied to {path} — skipping")

    if not already_exception:
        exc_count = src.count(EXCEPTION_ANCHOR)
        if exc_count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 EXCEPTION_ANCHOR in {path}, found "
                f"{exc_count} (upstream drifted — re-verify the MCP transport/auth "
                "exception handler in _make_tool_handler, the per-backend scoping "
                "may need to move)"
            )
        const_count = src.count(CONST_ANCHOR)
        if const_count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 CONST_ANCHOR in {path}, found "
                f"{const_count} (cannot safely inject _VICEGERENT_MCP_BACKEND_NAMES)"
            )
        src = src.replace(CONST_ANCHOR, BACKEND_NAMES_BLOCK + CONST_ANCHOR)
        src = src.replace(EXCEPTION_ANCHOR, EXCEPTION_REPLACEMENT)
    else:
        print(f"patch: per-backend scoping fix already applied to {path} — skipping")

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied MCP circuit-breaker fix(es) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
