#!/usr/bin/env python3
"""Vicegerent patch: don't trip the MCP circuit breaker on business errors.

Context
-------
tools/mcp_tool.py implements a circuit breaker per MCP server: after
_CIRCUIT_BREAKER_THRESHOLD (3) consecutive "failures" it opens and blocks
ALL tools on that server for _CIRCUIT_BREAKER_COOLDOWN_SEC (60s), telling
the model "MCP server '<name>' is unreachable ... Do NOT retry this tool".

The successful-call branch (after result = _call_once()) treats ANY
top-level "error" key in the parsed JSON as a circuit-breaker-worthy
failure — including MCP `result.isError = true` business errors (bad
arguments, upstream API auth/permission errors, 4xx responses relayed by
the tool, etc). Those indicate the *tool call* failed, not that the *MCP
server* is unreachable. In this environment we saw a GitLab MCP call
return 401 (expired token) then 403 (WAF) then a real transport timeout,
each counted identically toward the same 3-strike breaker, tripping it
and blocking retries even seconds after the underlying issue (token,
WAF) was fixed on the GitLab side.

Upstream tracks this as https://github.com/NousResearch/hermes-agent
issues #47851 / #11113, with fixes in flight (#47918, #47955) not yet
released as of 2026-06-30.

This patch changes only the successful-call JSON-error branch: a
business error (`"error"` key in a normally-returned JSON payload) now
resets the breaker instead of bumping it — the server clearly responded,
so it is reachable; the model should just retry with different
arguments/reasoning. The transport/auth exception branch further down
(except Exception as exc: ... _bump_server_error(server_name)) is left
untouched — actual connection failures, timeouts, and unrecovered
auth errors still bump the breaker as before.

Fail-loud by design: if the anchor is absent or appears more than once
(i.e. upstream refactored this path — possibly landing the real fix),
the patch raises and the image build fails, signalling a re-verify.

Remove this patch once upstream Hermes lands #47918/#47955 (or
equivalent) and stops bumping the breaker on business errors.
"""
import importlib.util
import sys

ANCHOR = (
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

REPLACEMENT = (
    "        try:\n"
    "            result = _call_once()\n"
    "            # Check if the MCP tool itself returned an error. A business\n"
    "            # error (isError=true relayed as a JSON \"error\" key) means the\n"
    "            # server responded and is reachable — reset the breaker rather\n"
    "            # than bump it. Only transport/auth exceptions (the except\n"
    "            # Exception branch below) indicate real server unreachability.\n"
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

# Idempotence marker: a unique token from the replacement so a re-run is a
# no-op rather than a hard failure (the anchor is gone after the first apply).
APPLIED_MARKER = "Vicegerent patch for hermes-agent #47851 / #11113."


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
            "(upstream drifted — re-verify the MCP circuit breaker path, "
            "the real fix for #47851/#11113 may have landed)"
        )

    src = src.replace(ANCHOR, REPLACEMENT)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied MCP circuit-breaker business-error fix to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
