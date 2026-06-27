#!/usr/bin/env python3
"""Vicegerent patch: keep MCP servers in the reconnect loop after initial
connection failure instead of permanently removing them from the session.

Upstream behaviour (tools/mcp_tool.py):
  When _MAX_INITIAL_CONNECT_RETRIES (3) are exhausted at startup, the server
  task sets self._ready (so Hermes startup doesn't block), sets self._error,
  then RETURNS — permanently killing the server for the session. If the host
  stack (ghostunnel, mcp-proxy-server) isn't up yet when Hermes starts, those
  MCPs are silently gone until the pod is restarted.

Patched behaviour:
  On initial-retry exhaustion, mark ready and clear _error (so startup
  continues and the server shows as "failed" rather than absent), then
  fall through to the normal post-ready reconnect loop. The server will
  keep retrying with exponential backoff (up to _MAX_RECONNECT_RETRIES = 5)
  and will come back online automatically once the upstream is available.

This makes vicegerent resilient to startup ordering: Hermes can start before
the host MCP stack is running and will reconnect once ghostunnel/caddy/proxy
come up, without needing a pod restart.

Fail-loud by design: if the anchor lines are absent or ambiguous, the patch
raises and the image build fails, signalling a re-verify against the new
upstream code.

Drop this patch once upstream adds a 'keep-retrying' or 'lazy-connect' option.
"""
import sys

# The exact block to replace — match must be unique
ANCHOR = """\
                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            \"MCP server '%s' failed initial connection after \"
                            \"%d attempts, giving up: %s\",
                            self.name, _MAX_INITIAL_CONNECT_RETRIES, exc,
                        )
                        self._error = exc
                        self._ready.set()
                        return
"""

REPLACEMENT = """\
                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            \"MCP server '%s' failed initial connection after \"
                            \"%d attempts, will keep retrying in background: %s\",
                            self.name, _MAX_INITIAL_CONNECT_RETRIES, exc,
                        )
                        # Mark ready so Hermes startup doesn't block, but
                        # do NOT return — fall through to the post-ready
                        # reconnect loop so the server comes back automatically
                        # once the upstream (host stack, remote service) is up.
                        self._error = exc
                        self._ready.set()
                        # Reset initial_retries so the reconnect counter takes
                        # over cleanly from here.
                        initial_retries = _MAX_INITIAL_CONNECT_RETRIES + 1
                        # fall through (no continue, no return)
"""

path = "/usr/local/lib/hermes-agent/tools/mcp_tool.py"

with open(path, "r") as f:
    src = f.read()

count = src.count(ANCHOR)
if count == 0:
    print(f"PATCH FAILED: anchor not found in {path}", file=sys.stderr)
    print("Upstream may have refactored this path — re-verify patch.", file=sys.stderr)
    sys.exit(1)
if count > 1:
    print(f"PATCH FAILED: anchor found {count} times in {path} (expected 1)", file=sys.stderr)
    sys.exit(1)

patched = src.replace(ANCHOR, REPLACEMENT, 1)

with open(path, "w") as f:
    f.write(patched)

print(f"Patched {path}: MCP initial-retry give-up → keep-retrying")
