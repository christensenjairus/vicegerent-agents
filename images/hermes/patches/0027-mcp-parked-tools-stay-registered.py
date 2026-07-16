#!/usr/bin/env python3
"""Vicegerent patch: MCP tools stay registered while a server is parked.

Context
-------
``MCPServerTask.run()`` (tools/mcp_tool.py) parks a server once its
reconnect budget is exhausted (``_MAX_RECONNECT_RETRIES = 5`` for a
mid-session drop, ``_MAX_INITIAL_CONNECT_RETRIES = 3`` for a server that
never connected in the first place). Both park paths call
``self._deregister_tools()`` before waiting, which empties the global
tool registry of every schema this server contributed. The practical
effect: the model's tool list silently shrinks/grows across turns
whenever vmcp (or any MCP server) has a rough patch, and comes back only
after the next successful self-probe (every ``_PARKED_RETRY_INTERVAL``
seconds, 60s per this repo's own patch 0022) re-registers them.

The user wants the tool list to NEVER change mid-session -- the model
should always see the same tools, whether or not the backing server is
currently reachable -- while leaving the 60s self-probe cadence and
retry budget completely untouched (faster retry / uncapped retry loops
were explicitly declined after being warned about busy-loop risk on a
genuine extended outage).

Related but independent: patches/0026-mcp-parked-stall-visibility.py (as
of this writing, still open as MR !540 against branch
fix/mcp-parked-stall-visibility, NOT yet merged to main) makes prolonged
parks LOUD in the logs via a ``_consecutive_park_cycles`` counter. This
patch is orthogonal -- it changes what happens to the tool REGISTRY
entries themselves while parked (they stay registered), not how loudly
parking is logged. The two patches touch different lines in the same
park blocks (0026 adds a counter increment/reset; this patch removes a
``_deregister_tools()`` call) but do not conflict in intent. Since 0026
has not merged as of this patch, this script does not need to coexist
with it in the same build yet -- if/when it does merge, a human should
confirm both patches' ANCHORs still apply cleanly against the then-
current file (they touch adjacent but distinct lines within the same
two park blocks).

Fix
---
This patch has three parts:

1. **Registry**: remove the ``self._deregister_tools()`` call from both
   park paths (the mid-session-drop park in ``run()``'s main exception
   handler, and the initial-connect-retry park a bit above it). Add a
   new ``self._parked`` boolean flag (``__slots__`` + ``__init__``,
   default ``False``) that is set ``True`` on entering either park path
   and reset ``False`` on every path that already resets
   ``self._reconnect_retries = 0`` after a live session is confirmed
   (mirrors the existing pattern exactly -- same reset sites, same
   trigger). ``shutdown()``'s own ``self._deregister_tools()`` call is
   untouched: a graceful, permanent shutdown must still clear the
   registry, only the two *parked-but-still-alive* paths change.

2. **Call path (``tools/call``)**: because tools now stay registered
   while parked, a tool call against a parked server reaches
   ``_make_tool_handler``'s ``_handler`` (tools/mcp_tool.py) instead of
   failing at a registry lookup. That handler already has a
   ``if not server.session:`` branch for a transiently-reconnecting
   server, which (after a short session-ready wait) calls
   ``_signal_reconnect(server)`` and returns a clean error telling the
   model to back off. Calling ``_signal_reconnect`` is correct for a
   genuinely mid-reconnect server, but WRONG for a parked one: it sets
   ``_reconnect_event`` immediately, which wakes
   ``_wait_for_reconnect_or_shutdown`` right away and forces an
   off-cadence reconnect attempt on every single tool call against a
   parked server -- exactly the busy-loop-on-outage behavior the user
   declined. This patch adds a ``self._parked`` check ahead of that
   branch: when parked, return a clean, distinct error immediately (no
   session-ready wait, no ``_signal_reconnect`` call, no retry by this
   code) so the self-probe timing in ``run()`` is completely unaffected
   by tool-call traffic. The model gets back a short, human-readable
   string distinguishing "parked, tools registered but temporarily
   unavailable, retry on a later turn" from both the generic
   transient-reconnect message and a raw transport exception.

3. **Fallthrough call-path polish (``tools/call``)** (added after live
   testing surfaced the gap): a live dogfood of this exact bug -- hitting
   ``vmcp``'s ``find_tool``/``list_resources`` mid-outage -- showed that
   most real failures never reach the new ``self._parked`` branch above
   at all. They land one level up, in ``_handler``'s generic
   ``except Exception`` fallthrough (after ``_handle_auth_error_and_retry``
   and ``_handle_session_expired_and_retry`` both decline/exhaust), which
   returns the bare
   ``f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"`` --
   e.g. verbatim ``"MCP call failed: ClosedResourceError:
   ClosedResourceError()"`` -- with no guidance at all. When that
   exception matches ``_is_session_expired_error``, reaching this
   fallthrough specifically means an automatic reconnect+retry was
   already attempted by ``_handle_session_expired_and_retry`` and it
   still failed -- a materially different, more informative situation
   than a fresh/unclassified exception, and one the model has zero
   signal to distinguish today. This patch adds an
   ``_is_session_expired_error`` check ahead of the generic fallthrough
   that (a) returns an actionable message telling the model recovery was
   already attempted and to back off rather than retry immediately, and
   (b) logs that distinction explicitly, so a future occurrence can be
   diagnosed from the logs instead of guessed at from call timing (as
   happened during this patch's own live verification).

4. **``_is_session_expired_error`` classifier bug** (found via a SECOND
   live re-test after part 3 shipped): with part 3 deployed, a live
   ``ClosedResourceError()`` from an actual vmcp outage STILL produced
   the exact same bare, unhelpful message as before -- neither the
   pre-existing ``_handle_session_expired_and_retry`` reconnect path nor
   the new part-3 fallthrough message ever fired. Root cause:
   ``_is_session_expired_error`` builds its match string with
   ``msg = str(exc).lower()`` and returns ``False`` immediately when
   ``msg`` is empty. ``anyio.ClosedResourceError`` (like several other
   stdlib/anyio exceptions) is raised with NO message argument, so
   ``str(exc) == ""`` -- the function bails out via the
   ``if not msg: return False`` guard before ever consulting
   ``_SESSION_EXPIRED_MARKERS``, even though ``"closedresourceerror"``
   is literally on that list. This is a PRE-EXISTING bug in
   ``_is_session_expired_error`` (not introduced by parts 1-3 of this
   patch), but it silently defeats both the original reconnect-and-retry
   recovery path AND this patch's own new fallthrough message for
   exactly the exception type this whole investigation started from.
   The file already has an ``_exc_str()`` helper built for precisely
   this situation (falls back to ``repr(exc)`` when ``str(exc)`` is
   empty, docstring literally calls out ``anyio.ClosedResourceError`` as
   the motivating case) -- ``_is_session_expired_error`` just never used
   it. This patch swaps ``str(exc).lower()`` for
   ``msg = _exc_str(exc).lower()`` so the empty-``str()`` case falls through
   to ``repr(exc)`` (e.g. ``"ClosedResourceError()"``, which still
   contains ``"closedresourceerror"``) instead of short-circuiting on an
   empty string. This is a SHARED helper -- fixing it here also fixes
   the equivalent gap in the four handlers touched by part 6 below.

5. **``_make_check_fn`` visibility gate bug** (found via a THIRD live
   re-test, after parts 1-4 shipped and were confirmed working end to
   end): with a real vmcp outage triggering a genuine park, ALL vmcp
   tools vanished from the model's tool list entirely -- calling them
   returned "Tool 'mcp__vmcp__find_tool' does not exist", not any of
   the clean error messages parts 2-4 added. This is the exact symptom
   parts 1-4 were built to eliminate, reintroduced through a SEPARATE
   mechanism parts 1-4 didn't touch. Root cause: every MCP tool is
   registered with a ``check_fn`` (built by ``_make_check_fn``, called
   at registration time) that ``registry.get_definitions()`` invokes on
   every tool-list build; a tool whose ``check_fn()`` returns ``False``
   is silently dropped from the returned definitions (registry entry
   untouched, just excluded from that call's output). ``_make_check_fn``'s
   ``_check()`` returns ``server is not None and (server.session is not
   None or server._is_recycled_stdio())`` -- i.e. it gates on a LIVE
   session, completely independent of the registry-membership fix in
   part 1. A parked server (by definition) has ``server.session is
   None``, so ``check_fn()`` returns ``False``, and the tool disappears
   from every ``get_tool_definitions()`` call -- including the
   between-turns ``refresh_agent_mcp_tools`` rebuild described in that
   function's own docstring -- even though part 1 kept it registered.
   Parts 1-4 fixed the deregistration path; this is a second,
   independent filtering path that produces the identical user-visible
   symptom (tool list changes mid-session during an outage) and was
   missed because the live test conditions (session-expired errors,
   short outages recovering within the reconnect budget) never actually
   drove a server all the way into the parked state until this third
   round. Fix: ``_check()`` now also treats ``getattr(server, "_parked",
   False)`` as visible -- a parked server's tools stay in the model's
   tool list (matching part 1's stated goal exactly), and a call against
   one still routes to the part-2 ``self._parked`` branch in
   ``_make_tool_handler`` for the clean "reconnecting" message, instead
   of failing at the tool-list level before the call is ever attempted.
   ``_make_check_fn`` is SHARED across every MCP tool regardless of
   which handler registered it (``tools/call``, ``resources/list``,
   ``resources/read``, ``prompts/list``, ``prompts/get``), so this one
   fix already restored tool-list visibility for all five call paths --
   part 6 below is purely about the ERROR MESSAGE quality once a call
   actually lands on a parked/failed server, not visibility.

6. **The same call-path polish extended to the other four MCP handlers**
   (cleanup pass, requested by the user after parts 1-5 were verified
   live end to end): parts 2-4 above were written and tested against
   ``_make_tool_handler`` (the ``tools/call`` bridge) ONLY. A live
   ``list_resources`` call during the same outage that exercised parts
   2-4 kept returning the flat, pre-patch ``"MCP server '{name}' is not
   connected"`` on every round of testing -- never any of the newer
   clean messages -- because ``_make_list_resources_handler``,
   ``_make_read_resource_handler``, ``_make_list_prompts_handler``, and
   ``_make_get_prompt_handler`` (tools/mcp_tool.py) each have their OWN
   independent copy of the same ``if not server or not server.session:``
   check and the same bare
   ``f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"``
   fallthrough that parts 2 and 3 fixed only in ``_make_tool_handler``.
   This part applies the identical two changes (parked-branch clean
   message ahead of the not-connected check; ``_is_session_expired_error``
   check ahead of the generic fallthrough) to all four of those handlers,
   so every MCP call surface gives the model the same actionable,
   consistent errors during an outage -- not just ``tools/call``. No new
   root-cause bug here (unlike parts 4-5): this is applying the ALREADY-
   VERIFIED parts 2 and 3 fixes to four handlers that were simply never
   in scope of the original patch, using the exact same logic and
   messages, parameterized only by the per-handler operation name used
   in log lines (``resources/list``, ``resources/read``,
   ``prompts/list``, ``prompts/get``).

Fail-loud by design: if any of the anchors below are absent or appear an
unexpected number of times (upstream changed shape), the patch raises
and the image build fails, signalling a re-verify.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0027"

# ---------------------------------------------------------------------
# 1. __slots__: add the new _parked flag
# ---------------------------------------------------------------------
SLOTS_ANCHOR = (
    "        \"initialize_result\", \"_ping_unsupported\",\n"
    "        \"_reconnect_retries\",\n"
    "    )\n"
)
SLOTS_REPLACEMENT = (
    "        \"initialize_result\", \"_ping_unsupported\",\n"
    "        \"_reconnect_retries\",\n"
    "        # Vicegerent patch 0027: True while the reconnect budget is\n"
    "        # exhausted and this server is parked. Tools stay registered\n"
    "        # while parked (see _deregister_tools call sites below); this\n"
    "        # flag lets a tool call against a parked server return a clean\n"
    "        # \"reconnecting, tools temporarily unavailable\" error instead\n"
    "        # of forcing an off-cadence reconnect via _signal_reconnect.\n"
    "        \"_parked\",\n"
    "    )\n"
)

# ---------------------------------------------------------------------
# 2. __init__: default the new flag to False
# ---------------------------------------------------------------------
INIT_ANCHOR = (
    "        self._reconnect_retries: int = 0\n"
    "        self._auth_type: str = \"\"\n"
)
INIT_REPLACEMENT = (
    "        self._reconnect_retries: int = 0\n"
    "        # Vicegerent patch 0027: see __slots__ comment above.\n"
    "        self._parked: bool = False\n"
    "        self._auth_type: str = \"\"\n"
)

# ---------------------------------------------------------------------
# 3. Reset _parked alongside every existing _reconnect_retries = 0 reset
#    that fires once a live session is confirmed. Six distinct sites,
#    each anchored with enough surrounding context to be unique.
# ---------------------------------------------------------------------
RESET_ANCHORS = [
    (
        "stdio-revival-comment",
        (
            "                    # This session is live: reset the reconnect retry counter\n"
            "                    # so transient prior failures do not accumulate toward\n"
            "                    # permanent parking (#57604).\n"
            "                    self._reconnect_retries = 0\n"
        ),
        (
            "                    # This session is live: reset the reconnect retry counter\n"
            "                    # so transient prior failures do not accumulate toward\n"
            "                    # permanent parking (#57604).\n"
            "                    self._reconnect_retries = 0\n"
            "                    # Vicegerent patch 0027: tools stay registered while\n"
            "                    # parked, so clear the parked flag alongside the retry\n"
            "                    # counter reset -- this is a live, non-parked session again.\n"
            "                    self._parked = False\n"
        ),
    ),
    (
        "sse-teardown",
        (
            "                    _reset_server_error(self.name)\n"
            "                    self._reconnect_retries = 0\n"
            "                    reason = await self._wait_for_lifecycle_event()\n"
            "                    if reason == \"reconnect\":\n"
            "                        logger.info(\n"
            "                            \"MCP server '%s': reconnect requested — \"\n"
            "                            \"tearing down SSE session\", self.name,\n"
            "                        )\n"
        ),
        (
            "                    _reset_server_error(self.name)\n"
            "                    self._reconnect_retries = 0\n"
            "                    self._parked = False  # Vicegerent patch 0027\n"
            "                    reason = await self._wait_for_lifecycle_event()\n"
            "                    if reason == \"reconnect\":\n"
            "                        logger.info(\n"
            "                            \"MCP server '%s': reconnect requested — \"\n"
            "                            \"tearing down SSE session\", self.name,\n"
            "                        )\n"
        ),
    ),
    (
        "http-teardown",
        (
            "                        _reset_server_error(self.name)\n"
            "                        self._reconnect_retries = 0\n"
            "                        reason = await self._wait_for_lifecycle_event()\n"
            "                        if reason == \"reconnect\":\n"
            "                            logger.info(\n"
            "                                \"MCP server '%s': reconnect requested — \"\n"
            "                                \"tearing down HTTP session\", self.name,\n"
            "                            )\n"
        ),
        (
            "                        _reset_server_error(self.name)\n"
            "                        self._reconnect_retries = 0\n"
            "                        self._parked = False  # Vicegerent patch 0027\n"
            "                        reason = await self._wait_for_lifecycle_event()\n"
            "                        if reason == \"reconnect\":\n"
            "                            logger.info(\n"
            "                                \"MCP server '%s': reconnect requested — \"\n"
            "                                \"tearing down HTTP session\", self.name,\n"
            "                            )\n"
        ),
    ),
    (
        "legacy-http-teardown",
        (
            "                    _reset_server_error(self.name)\n"
            "                    self._reconnect_retries = 0\n"
            "                    reason = await self._wait_for_lifecycle_event()\n"
            "                    if reason == \"reconnect\":\n"
            "                        logger.info(\n"
            "                            \"MCP server '%s': reconnect requested — \"\n"
            "                            \"tearing down legacy HTTP session\", self.name,\n"
            "                        )\n"
        ),
        (
            "                    _reset_server_error(self.name)\n"
            "                    self._reconnect_retries = 0\n"
            "                    self._parked = False  # Vicegerent patch 0027\n"
            "                    reason = await self._wait_for_lifecycle_event()\n"
            "                    if reason == \"reconnect\":\n"
            "                        logger.info(\n"
            "                            \"MCP server '%s': reconnect requested — \"\n"
            "                            \"tearing down legacy HTTP session\", self.name,\n"
            "                        )\n"
        ),
    ),
    (
        "clean-transport-return",
        (
            "                self._reconnect_retries = 0\n"
            "                backoff = 1.0\n"
            "                # Reset the session reference and readiness; _run_http/_run_stdio\n"
        ),
        (
            "                self._reconnect_retries = 0\n"
            "                self._parked = False  # Vicegerent patch 0027\n"
            "                backoff = 1.0\n"
            "                # Reset the session reference and readiness; _run_http/_run_stdio\n"
        ),
    ),
    (
        "initial-connect-revival",
        (
            "                        initial_retries = 0\n"
            "                        self._reconnect_retries = 0\n"
            "                        backoff = 1.0\n"
            "                        self._error = None\n"
            "                        self._ready.clear()\n"
            "                        continue\n"
        ),
        (
            "                        initial_retries = 0\n"
            "                        self._reconnect_retries = 0\n"
            "                        self._parked = False  # Vicegerent patch 0027\n"
            "                        backoff = 1.0\n"
            "                        self._error = None\n"
            "                        self._ready.clear()\n"
            "                        continue\n"
        ),
    ),
]

# ---------------------------------------------------------------------
# 4. Initial-connect-retry park path: stop deregistering tools, set _parked
# ---------------------------------------------------------------------
INITIAL_PARK_ANCHOR = (
    "                        self._error = exc\n"
    "                        self._ready.set()\n"
    "                        self._deregister_tools()\n"
    "                        self._reconnect_event.clear()\n"
)
INITIAL_PARK_REPLACEMENT = (
    "                        self._error = exc\n"
    "                        self._ready.set()\n"
    "                        # Vicegerent patch 0027: do NOT deregister tools while\n"
    "                        # parked -- the model's tool list must never change.\n"
    "                        # A tool call reaching _make_tool_handler while\n"
    "                        # self._parked is True gets a clean error instead of\n"
    "                        # a registry lookup failure; see that function.\n"
    "                        self._parked = True\n"
    "                        self._reconnect_event.clear()\n"
)

# ---------------------------------------------------------------------
# 5. Main park path (reconnect budget exhausted mid-session)
# ---------------------------------------------------------------------
MAIN_PARK_ANCHOR = (
    "                    # Do NOT return — exiting the task orphans the server:\n"
    "                    # nothing would ever listen for _reconnect_event again\n"
    "                    # and the server would be permanently wedged for the\n"
    "                    # life of the process (#16788). Instead, drop the phantom\n"
    "                    # tools from the registry and park. Because parking\n"
    "                    # deregisters the tools, no tool call can reach the\n"
    "                    # circuit-breaker half-open probe or _signal_reconnect —\n"
    "                    # so the park is a TIMED wait: every _PARKED_RETRY_INTERVAL\n"
    "                    # we wake and attempt one reconnect ourselves (#57129).\n"
    "                    # An explicit _reconnect_event.set() (OAuth recovery,\n"
    "                    # manual /mcp refresh) still wakes us immediately.\n"
    "                    self._deregister_tools()\n"
    "                    self._reconnect_event.clear()\n"
)
MAIN_PARK_REPLACEMENT = (
    "                    # Do NOT return — exiting the task orphans the server:\n"
    "                    # nothing would ever listen for _reconnect_event again\n"
    "                    # and the server would be permanently wedged for the\n"
    "                    # life of the process (#16788). Park instead.\n"
    "                    #\n"
    "                    # Vicegerent patch 0027: tools are intentionally NOT\n"
    "                    # deregistered here anymore -- the model's tool list\n"
    "                    # must never change mid-session. A tool call against a\n"
    "                    # parked server now reaches _make_tool_handler, which\n"
    "                    # sees self.session is None, checks the self._parked\n"
    "                    # flag set below, and returns a clean \"reconnecting,\n"
    "                    # try again later\" error WITHOUT calling\n"
    "                    # _signal_reconnect -- so the self-probe cadence below\n"
    "                    # is unaffected by tool-call traffic and stays a TIMED\n"
    "                    # wait: every _PARKED_RETRY_INTERVAL we wake and attempt\n"
    "                    # one reconnect ourselves (#57129). An explicit\n"
    "                    # _reconnect_event.set() (OAuth recovery, manual /mcp\n"
    "                    # refresh) still wakes us immediately, same as before.\n"
    "                    self._parked = True\n"
    "                    self._reconnect_event.clear()\n"
)

# ---------------------------------------------------------------------
# 6. _deregister_tools docstring: correct the now-stale claim that it
#    runs on shutdown AND when the reconnect budget is exhausted.
# ---------------------------------------------------------------------
DEREGISTER_DOCSTRING_ANCHOR = (
    "        \"\"\"Drop this server's tools from the global registry (idempotent).\n"
    "\n"
    "        Pulls the server's tool schemas out of the registry so the agent\n"
    "        stops advertising them to the model. Called on shutdown AND when the\n"
    "        reconnect budget is exhausted, so a dead server never leaves phantom\n"
    "        tool definitions bloating the prompt cache and producing \"not\n"
    "        connected\" errors on every turn.\n"
    "        \"\"\"\n"
)
DEREGISTER_DOCSTRING_REPLACEMENT = (
    "        \"\"\"Drop this server's tools from the global registry (idempotent).\n"
    "\n"
    "        Pulls the server's tool schemas out of the registry so the agent\n"
    "        stops advertising them to the model. Called ONLY on a genuine,\n"
    "        permanent shutdown() (Vicegerent patch 0027 -- previously also\n"
    "        called when the reconnect budget was exhausted; that call was\n"
    "        removed so the model's tool list never changes while a server is\n"
    "        merely parked and self-probing, only when it's actually being\n"
    "        torn down for good).\n"
    "        \"\"\"\n"
)

# ---------------------------------------------------------------------
# 7. Call path: clean error for a parked server, without forcing an
#    off-cadence reconnect via _signal_reconnect.
# ---------------------------------------------------------------------
CALL_PATH_ANCHOR = (
    "        if not server.session:\n"
    "            # No live session. A reconnect may already be completing (the\n"
    "            # transport swaps in a fresh session object asynchronously) —\n"
    "            # wait briefly before treating this as a failure, so a\n"
    "            # transient reconnect window doesn't burn a circuit-breaker\n"
    "            # strike (#26892).\n"
    "            if _wait_for_server_session_ready(\n"
)
CALL_PATH_REPLACEMENT = (
    "        if not server.session:\n"
    "            # Vicegerent patch 0027: a parked server (reconnect budget\n"
    "            # exhausted) keeps its tools registered now, so a call can\n"
    "            # land here while self._parked is True. Handle that FIRST,\n"
    "            # before the transient-reconnect wait below: waiting up to\n"
    "            # 5s only makes sense for a session that might come back\n"
    "            # within that window, and a parked server won't (it only\n"
    "            # self-probes every _PARKED_RETRY_INTERVAL == 60s). Return\n"
    "            # promptly with a clean, distinct message -- do NOT call\n"
    "            # _signal_reconnect here, since that would force an\n"
    "            # off-cadence reconnect attempt on every tool call against a\n"
    "            # parked server, defeating the fixed 60s self-probe interval\n"
    "            # and reintroducing exactly the busy-loop risk that was\n"
    "            # explicitly declined for this fix.\n"
    "            if getattr(server, \"_parked\", False):\n"
    "                _bump_server_error(server_name)\n"
    "                return json.dumps({\n"
    "                    \"error\": (\n"
    "                        f\"MCP server '{server_name}' is currently \"\n"
    "                        f\"reconnecting (parked after repeated connection \"\n"
    "                        f\"failures). Its tools remain registered but are \"\n"
    "                        f\"temporarily unavailable. The server self-probes \"\n"
    "                        f\"periodically on its own; wait and retry this \"\n"
    "                        f\"tool call on a later turn rather than retrying \"\n"
    "                        f\"immediately.\"\n"
    "                    )\n"
    "                }, ensure_ascii=False)\n"
    "            # No live session. A reconnect may already be completing (the\n"
    "            # transport swaps in a fresh session object asynchronously) —\n"
    "            # wait briefly before treating this as a failure, so a\n"
    "            # transient reconnect window doesn't burn a circuit-breaker\n"
    "            # strike (#26892).\n"
    "            if _wait_for_server_session_ready(\n"
)

# ---------------------------------------------------------------------
# 8. Generic exception fallthrough in the same call-path handler: give a
#    session-expired-and-retry-exhausted failure its own actionable
#    message + log line instead of a bare exception string. Surfaced by
#    live testing: this is the path that actually fires most often
#    (before the reconnect budget is exhausted enough to park).
# ---------------------------------------------------------------------
FALLTHROUGH_ANCHOR = (
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
    "            }, ensure_ascii=False)\n"
    "\n"
    "    return _handler\n"
)
FALLTHROUGH_REPLACEMENT = (
    "            breaker_key = server_name\n"
    "            if tool_name == \"call_tool\" and isinstance(args, dict):\n"
    "                real_tool_name = args.get(\"tool_name\")\n"
    "                if isinstance(real_tool_name, str) and real_tool_name:\n"
    "                    backend_prefix = real_tool_name.split(\"_\", 1)[0]\n"
    "                    if backend_prefix in _VICEGERENT_MCP_BACKEND_NAMES:\n"
    "                        breaker_key = f\"{server_name}:{backend_prefix}\"\n"
    "            _bump_server_error(breaker_key)\n"
    "            # Vicegerent patch 0027: a session-expired error reaching this\n"
    "            # fallthrough means _handle_session_expired_and_retry (above)\n"
    "            # already attempted a reconnect + retry and it still failed --\n"
    "            # a materially different situation than a fresh/unclassified\n"
    "            # exception. Log that distinction explicitly (so a future\n"
    "            # occurrence can be diagnosed from logs, not guessed from call\n"
    "            # timing) and give the model an actionable message instead of\n"
    "            # the bare exception string.\n"
    "            if _is_session_expired_error(exc):\n"
    "                logger.error(\n"
    "                    \"MCP tool %s/%s call failed after session-expired \"\n"
    "                    \"reconnect+retry already attempted and failed: %s\",\n"
    "                    server_name, tool_name, exc,\n"
    "                )\n"
    "                return json.dumps({\n"
    "                    \"error\": (\n"
    "                        f\"MCP server '{server_name}' transport session \"\n"
    "                        f\"expired and an automatic reconnect+retry already \"\n"
    "                        f\"failed ({type(exc).__name__}). Do NOT retry this \"\n"
    "                        f\"tool immediately -- wait for the next turn.\"\n"
    "                    )\n"
    "                }, ensure_ascii=False)\n"
    "            logger.error(\n"
    "                \"MCP tool %s/%s call failed: %s\",\n"
    "                server_name, tool_name, exc,\n"
    "            )\n"
    "            return json.dumps({\n"
    "                \"error\": _sanitize_error(\n"
    "                    f\"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}\"\n"
    "                )\n"
    "            }, ensure_ascii=False)\n"
    "\n"
    "    return _handler\n"
)


# ---------------------------------------------------------------------
# 9. _is_session_expired_error classifier bug: str(exc) is empty for
#    message-less exceptions (e.g. anyio.ClosedResourceError()), so the
#    "if not msg: return False" guard fires before _SESSION_EXPIRED_MARKERS
#    is ever consulted -- even though "closedresourceerror" is on that
#    list. Use the file's own _exc_str() helper (falls back to repr(exc))
#    instead of bare str(exc).
# ---------------------------------------------------------------------
CLASSIFIER_ANCHOR = (
    "    if isinstance(exc, InterruptedError):\n"
    "        return False\n"
    "    # Exception messages vary across SDK versions + server\n"
    "    # implementations, so match on a small allow-list of stable\n"
    "    # substrings rather than exception type.  Kept narrow to avoid\n"
    "    # false positives on unrelated server errors.\n"
    "    msg = str(exc).lower()\n"
    "    if not msg:\n"
    "        return False\n"
    "    return any(marker in msg for marker in _SESSION_EXPIRED_MARKERS)\n"
)
CLASSIFIER_REPLACEMENT = (
    "    if isinstance(exc, InterruptedError):\n"
    "        return False\n"
    "    # Exception messages vary across SDK versions + server\n"
    "    # implementations, so match on a small allow-list of stable\n"
    "    # substrings rather than exception type.  Kept narrow to avoid\n"
    "    # false positives on unrelated server errors.\n"
    "    #\n"
    "    # Vicegerent patch 0027: use _exc_str(exc) instead of bare str(exc).\n"
    "    # Several exceptions relevant here (notably anyio.ClosedResourceError,\n"
    "    # which is directly on _SESSION_EXPIRED_MARKERS below) are raised with\n"
    "    # NO message argument, so str(exc) == \"\" and the empty-msg guard used\n"
    "    # to return False before ever checking the marker list -- silently\n"
    "    # defeating this classifier for exactly the exception type it names.\n"
    "    # _exc_str() falls back to repr(exc) (e.g. \"ClosedResourceError()\",\n"
    "    # which still contains \"closedresourceerror\") when str(exc) is empty.\n"
    "    msg = _exc_str(exc).lower()\n"
    "    if not msg:\n"
    "        return False\n"
    "    return any(marker in msg for marker in _SESSION_EXPIRED_MARKERS)\n"
)


# ---------------------------------------------------------------------
# 10. _make_check_fn visibility gate: registry.get_definitions() drops any
#     tool whose check_fn() returns False, INDEPENDENTLY of whether it's
#     still registered. The MCP check_fn gated purely on a live session,
#     so a parked server's tools vanished from the model's tool list even
#     though part 1 kept them in the registry. Treat "_parked" as visible
#     too, matching part 1's stated goal.
# ---------------------------------------------------------------------
CHECK_FN_ANCHOR = (
    "def _make_check_fn(server_name: str):\n"
    "    \"\"\"Return a check function that verifies the MCP connection is alive.\"\"\"\n"
    "\n"
    "    def _check() -> bool:\n"
    "        with _lock:\n"
    "            server = _servers.get(server_name)\n"
    "        return (\n"
    "            server is not None\n"
    "            and (server.session is not None or server._is_recycled_stdio())\n"
    "        )\n"
    "\n"
    "    return _check\n"
)
CHECK_FN_REPLACEMENT = (
    "def _make_check_fn(server_name: str):\n"
    "    \"\"\"Return a check function that verifies the MCP connection is alive.\"\"\"\n"
    "\n"
    "    def _check() -> bool:\n"
    "        with _lock:\n"
    "            server = _servers.get(server_name)\n"
    "        # Vicegerent patch 0027: registry.get_definitions() drops any tool\n"
    "        # whose check_fn() returns False from the returned tool list, even\n"
    "        # though the registry ENTRY is untouched -- an independent filtering\n"
    "        # path from _deregister_tools() (see that method's docstring). A\n"
    "        # parked server (session is None by definition) used to fail this\n"
    "        # check and vanish from the model's tool list on every subsequent\n"
    "        # get_tool_definitions()/refresh_agent_mcp_tools() call, silently\n"
    "        # reintroducing the exact symptom part 1 was built to eliminate.\n"
    "        # Treat a parked server as visible too, matching part 1's goal that\n"
    "        # the tool list never changes mid-session while merely parked.\n"
    "        return (\n"
    "            server is not None\n"
    "            and (\n"
    "                server.session is not None\n"
    "                or server._is_recycled_stdio()\n"
    "                or getattr(server, \"_parked\", False)\n"
    "            )\n"
    "        )\n"
    "\n"
    "    return _check\n"
)


# ---------------------------------------------------------------------
# 11. Extend the same two call-path fixes (parked-branch clean message +
#     _is_session_expired_error fallthrough) from _make_tool_handler to
#     the four other MCP call handlers, which each have their own
#     independent copy of the "not connected" check and the bare
#     fallthrough message. Built parametrically since the four handlers
#     are structurally identical apart from the op_description string
#     used in the failed-call log line.
# ---------------------------------------------------------------------
_OTHER_HANDLER_OPS = [
    ("resources/list", "list_resources", "_make_list_resources_handler",
     "Return a sync handler that lists resources from an MCP server."),
    ("resources/read", "read_resource", "_make_read_resource_handler",
     "Return a sync handler that reads a resource by URI from an MCP server."),
    ("prompts/list", "list_prompts", "_make_list_prompts_handler",
     "Return a sync handler that lists prompts from an MCP server."),
    ("prompts/get", "get_prompt", "_make_get_prompt_handler",
     "Return a sync handler that gets a prompt by name from an MCP server."),
]

OTHER_HANDLER_ANCHORS = []
for _op_description, _log_label, _fn_name, _docstring in _OTHER_HANDLER_OPS:
    # Anchor spans from the handler's own def+docstring line (unique across
    # the file) through the "not connected" check body -- the check body
    # alone is byte-identical across all four handlers, so it must be
    # combined with the unique prefix to guarantee exactly 1 match.
    _not_connected_anchor = (
        f"def {_fn_name}(server_name: str, tool_timeout: float):\n"
        f"    \"\"\"{_docstring}\"\"\"\n"
        "\n"
        "    def _handler(args: dict, **kwargs) -> str:\n"
    )
    if _fn_name in ("_make_read_resource_handler", "_make_get_prompt_handler"):
        # These two handlers import tool_error before the server lookup.
        _not_connected_anchor += "        from tools.registry import tool_error\n\n"
    _not_connected_anchor += (
        "        server = _get_connected_server_for_call(server_name)\n"
        "        if not server or not server.session:\n"
        "            return json.dumps({\n"
        "                \"error\": f\"MCP server '{server_name}' is not connected\"\n"
        "            }, ensure_ascii=False)\n"
    )
    _not_connected_replacement_head = (
        f"def {_fn_name}(server_name: str, tool_timeout: float):\n"
        f"    \"\"\"{_docstring}\"\"\"\n"
        "\n"
        "    def _handler(args: dict, **kwargs) -> str:\n"
    )
    if _fn_name in ("_make_read_resource_handler", "_make_get_prompt_handler"):
        _not_connected_replacement_head += "        from tools.registry import tool_error\n\n"
    _not_connected_replacement = _not_connected_replacement_head + (
        "        server = _get_connected_server_for_call(server_name)\n"
        "        # Vicegerent patch 0027: a parked server (reconnect budget\n"
        "        # exhausted) keeps its tools registered now (see\n"
        "        # _make_check_fn), so a call can land here with server.session\n"
        "        # is None while server._parked is True. Handle that FIRST,\n"
        "        # matching the equivalent branch in _make_tool_handler --\n"
        "        # return a clean, distinct message instead of the generic\n"
        "        # \"not connected\" error, without forcing an off-cadence\n"
        "        # reconnect attempt (no _signal_reconnect call here).\n"
        "        if server is not None and getattr(server, \"_parked\", False):\n"
        "            _bump_server_error(server_name)\n"
        "            return json.dumps({\n"
        "                \"error\": (\n"
        "                    f\"MCP server '{server_name}' is currently \"\n"
        "                    f\"reconnecting (parked after repeated connection \"\n"
        "                    f\"failures). Its tools remain registered but are \"\n"
        "                    f\"temporarily unavailable. The server self-probes \"\n"
        "                    f\"periodically on its own; wait and retry this \"\n"
        "                    f\"tool call on a later turn rather than retrying \"\n"
        "                    f\"immediately.\"\n"
        "                )\n"
        "            }, ensure_ascii=False)\n"
        "        if not server or not server.session:\n"
        "            return json.dumps({\n"
        "                \"error\": f\"MCP server '{server_name}' is not connected\"\n"
        "            }, ensure_ascii=False)\n"
    )
    _fallthrough_anchor = (
        "            logger.error(\n"
        f"                \"MCP %s/{_log_label} failed: %s\", server_name, exc,\n"
        "            )\n"
        "            return json.dumps({\n"
        "                \"error\": _sanitize_error(\n"
        "                    f\"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}\"\n"
        "                )\n"
        "            }, ensure_ascii=False)\n"
        "\n"
        "    return _handler\n"
    )
    _fallthrough_replacement = (
        "            # Vicegerent patch 0027: a session-expired error reaching\n"
        "            # this fallthrough means _handle_session_expired_and_retry\n"
        "            # (above) already attempted a reconnect + retry and it\n"
        "            # still failed. Give the model an actionable message\n"
        "            # instead of the bare exception string (mirrors the\n"
        "            # equivalent fix in _make_tool_handler).\n"
        "            if _is_session_expired_error(exc):\n"
        "                logger.error(\n"
        f"                    \"MCP %s/{_log_label} failed after session-expired \"\n"
        "                    \"reconnect+retry already attempted and failed: %s\",\n"
        "                    server_name, exc,\n"
        "                )\n"
        "                return json.dumps({\n"
        "                    \"error\": (\n"
        "                        f\"MCP server '{server_name}' transport session \"\n"
        "                        f\"expired and an automatic reconnect+retry already \"\n"
        "                        f\"failed ({type(exc).__name__}). Do NOT retry this \"\n"
        "                        f\"tool immediately -- wait for the next turn.\"\n"
        "                    )\n"
        "                }, ensure_ascii=False)\n"
        "            logger.error(\n"
        f"                \"MCP %s/{_log_label} failed: %s\", server_name, exc,\n"
        "            )\n"
        "            return json.dumps({\n"
        "                \"error\": _sanitize_error(\n"
        "                    f\"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}\"\n"
        "                )\n"
        "            }, ensure_ascii=False)\n"
        "\n"
        "    return _handler\n"
    )
    OTHER_HANDLER_ANCHORS.append((
        _log_label,
        _not_connected_anchor, _not_connected_replacement,
        _fallthrough_anchor, _fallthrough_replacement,
    ))


def _count_or_raise(src: str, anchor: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 '{label}' anchor, found {count} "
            "(upstream drifted — re-verify)"
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

    _count_or_raise(src, SLOTS_ANCHOR, "__slots__")
    _count_or_raise(src, INIT_ANCHOR, "__init__")
    for label, anchor, _repl in RESET_ANCHORS:
        _count_or_raise(src, anchor, f"reset-site:{label}")
    _count_or_raise(src, INITIAL_PARK_ANCHOR, "initial-connect-park")
    _count_or_raise(src, MAIN_PARK_ANCHOR, "main-park")
    _count_or_raise(src, DEREGISTER_DOCSTRING_ANCHOR, "_deregister_tools docstring")
    _count_or_raise(src, CALL_PATH_ANCHOR, "call-path")
    _count_or_raise(src, FALLTHROUGH_ANCHOR, "call-path-fallthrough")
    _count_or_raise(src, CLASSIFIER_ANCHOR, "session-expired-classifier")
    _count_or_raise(src, CHECK_FN_ANCHOR, "mcp-check-fn-visibility-gate")
    for _log_label, _nc_anchor, _nc_repl, _ft_anchor, _ft_repl in OTHER_HANDLER_ANCHORS:
        _count_or_raise(src, _nc_anchor, f"not-connected:{_log_label}")
        _count_or_raise(src, _ft_anchor, f"fallthrough:{_log_label}")

    src = src.replace(SLOTS_ANCHOR, SLOTS_REPLACEMENT, 1)
    src = src.replace(INIT_ANCHOR, INIT_REPLACEMENT, 1)
    for _label, anchor, repl in RESET_ANCHORS:
        src = src.replace(anchor, repl, 1)
    src = src.replace(INITIAL_PARK_ANCHOR, INITIAL_PARK_REPLACEMENT, 1)
    src = src.replace(MAIN_PARK_ANCHOR, MAIN_PARK_REPLACEMENT, 1)
    src = src.replace(DEREGISTER_DOCSTRING_ANCHOR, DEREGISTER_DOCSTRING_REPLACEMENT, 1)
    src = src.replace(CALL_PATH_ANCHOR, CALL_PATH_REPLACEMENT, 1)
    src = src.replace(FALLTHROUGH_ANCHOR, FALLTHROUGH_REPLACEMENT, 1)
    src = src.replace(CLASSIFIER_ANCHOR, CLASSIFIER_REPLACEMENT, 1)
    src = src.replace(CHECK_FN_ANCHOR, CHECK_FN_REPLACEMENT, 1)
    for _log_label, _nc_anchor, _nc_repl, _ft_anchor, _ft_repl in OTHER_HANDLER_ANCHORS:
        src = src.replace(_nc_anchor, _nc_repl, 1)
        src = src.replace(_ft_anchor, _ft_repl, 1)

    # Stamp the applied marker (via the module docstring header, first line
    # already present at file top is untouched; append a lightweight marker
    # comment right after the main-park replacement, which is unique and
    # always present once this patch has run).
    marker_anchor = "                    self._parked = True\n                    self._reconnect_event.clear()\n"
    if marker_anchor not in src:
        raise SystemExit("patch: could not locate post-replacement anchor to stamp marker")
    src = src.replace(
        marker_anchor,
        marker_anchor + f"                    # {APPLIED_MARKER}\n",
        1,
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: MCP tools no longer deregistered while parked in {path} "
        "(registry stays populated; parked tool calls now return a clean "
        "'reconnecting' error instead of a registry-lookup failure; a "
        "session-expired-retry-exhausted fallthrough now also returns an "
        "actionable message instead of a bare exception string; "
        "_is_session_expired_error now uses _exc_str() so message-less "
        "exceptions like ClosedResourceError() are classified correctly; "
        "the MCP check_fn visibility gate now also treats a parked server "
        "as visible, so its tools stay in the model's tool list instead of "
        "silently vanishing from get_tool_definitions()/refresh_agent_mcp_tools(); "
        "the parked-branch and session-expired-fallthrough messages are now "
        "also applied to list_resources/read_resource/list_prompts/get_prompt, "
        "not just tools/call)"
    )


def main() -> int:
    _patch_mcp_tool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
