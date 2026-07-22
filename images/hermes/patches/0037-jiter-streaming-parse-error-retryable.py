#!/usr/bin/env python3
"""Treat jiter's bare ValueError on malformed streamed tool-call JSON as
retryable, not a local programming bug -- in BOTH places Hermes classifies
this error family.

Context
-------
``agent/conversation_loop.py``'s error classifier already special-cases
``json.JSONDecodeError`` — the comment right above ``is_local_validation_error``
says explicitly: "it indicates a transient provider/network failure
(malformed response body, truncated stream, routing layer corruption), not
a local programming bug, and should be retried (#14782)".

But Hermes prefers the *streaming* Anthropic call even for non-streaming
turns (``agent/anthropic_adapter.py::create_anthropic_message``,
``prefer_stream=True``), and the Anthropic SDK's own streaming snapshot
builder (``anthropic/lib/streaming/_messages.py``) reconstructs tool-call
arguments incrementally via a SEPARATE JSON parser:

    from jiter import from_json
    json_buf += bytes(event.delta.partial_json, "utf-8")
    content.input = from_json(json_buf, partial_mode=True)

``jiter`` is a Rust-based parser, not stdlib ``json``. When the
reassembled buffer is malformed for any reason upstream of Hermes
(a mid-stream promptGuard rewrite, dropped/reordered SSE frames, any
proxy-layer corruption between agentgateway and Hermes), ``jiter.from_json()``
raises a PLAIN ``ValueError`` — e.g. "expected value at line 1 column 87"
or "expected ident at line 1 column 2" — that does NOT inherit from
``json.JSONDecodeError``. Confirmed locally:

    >>> import jiter; jiter.from_json(b'garbage')
    ValueError: expected ident at line 1 column 1

Live incident 1 (2026-07-21): a Slack turn's primary Anthropic call
returned HTTP 200 (confirmed via agentgateway's own access log — 3932ms,
202 output tokens) but Hermes's client raised ``ValueError: expected value
at line 1 column 87`` parsing it. ``conversation_loop.py``'s
``is_local_validation_error`` classifier misrouted this to the ``gpt-5.4``
fallback, which then hard-400'd on an unrelated ``reasoning_effort``-on-
``/chat/completions`` bug (fixed separately, MR !604). This patch's first
half fixes that decision.

Live incident 2 (2026-07-22, SAME turn shape recurring, different code
path): another turn hit ``ValueError: expected value at line 1 column 98``
mid-stream, this time via a SECOND, narrower classifier that this patch
originally missed: ``run_agent.py::_is_provider_stream_parse_error``, which
gates the silent-mid-stream-retry decision in
``agent/chat_completion_helpers.py`` (the ``deltas_were_sent`` branch, used
when a tool call is in-flight when the stream dies). That function only
recognized the literal substring "expected ident at line" -- not "expected
value at line" (what actually fired) nor the other jiter parse-failure
shapes. Missing the match, it fell through to the "not retrying" stub path,
which in turn feeds ``conversation_loop.py``'s eager-fallback logic
("empty/malformed responses are a common rate-limit symptom, switch to
fallback immediately") -- reproducing the exact same failure class MR !604
was supposed to close, because the actual TRIGGER (jiter parse error
misclassified as non-retryable) was never fixed at its source; only the
fallback's own crash-on-400 (a downstream symptom) was. This patch's
second half fixes ``_is_provider_stream_parse_error`` to use the SAME
marker list as ``is_local_validation_error``, so both classifiers agree on
what counts as a retryable jiter hiccup.

There's a partial, disconnected acknowledgment of jiter's error shape
already in the codebase: ``run_agent.py``'s ``_summarize_api_error`` special-
cases the substring "expected ident at line" to produce a nicer log message
("Malformed provider streaming response: ..."). That has ZERO effect on
either retry/fallback decision — it only prettifies what gets logged after
the (wrong) decision was already made. This patch fixes both actual
decisions.

Fix
---
Two independent, idempotent sub-patches applied by the same script (kept
together because they're the same root cause and the same fix pattern --
message-substring jiter-parse-error detection -- just at two call sites):

1. ``agent/conversation_loop.py::is_local_validation_error`` — add the
   jiter-parse-error exclusion (unchanged from the original 0037; documented
   above as "live incident 1").
2. ``run_agent.py::_is_provider_stream_parse_error`` — widen its single
   hardcoded substring check ("expected ident at line") to the same 4-marker
   list used above, so a jiter parse error hit mid-stream (tool call
   in-flight) gets one same-provider silent retry instead of immediately
   falling through to the stub-response / eager-fallback path.

Markers cover every jiter parse-failure shape observed in its own error
strings ("expected value/ident at line...", "EOF while parsing...",
"trailing characters at line..."). A false-positive match against a genuine
local ``ValueError`` unrelated to JSON parsing is essentially impossible
given how specific jiter's phrasing is, and worst case only causes one
extra same-provider retry before the normal retry/fallback machinery still
runs.

Fail-loud by design: if either anchor is absent or appears an unexpected
number of times (upstream refactored either classifier), the patch raises
and the image build fails, signalling a re-verify. Idempotent: a re-run
after a successful apply is a no-op for each half independently.

Remove once upstream either (a) makes both classifiers check jiter's actual
exception type instead of message substrings, or (b) the Anthropic SDK
wraps jiter parse failures in a dedicated, catchable exception type.
"""
import importlib.util
import sys

APPLIED_MARKER_1 = "Vicegerent patch 0037"
APPLIED_MARKER_2 = "Vicegerent patch 0037 (part 2"

# --- Sub-patch 1: agent/conversation_loop.py ---------------------------

ANCHOR_1 = (
    "                is_local_validation_error = (\n"
    "                    isinstance(api_error, (ValueError, TypeError))\n"
    "                    and not isinstance(\n"
    "                        api_error, (UnicodeEncodeError, json.JSONDecodeError)\n"
    "                    )\n"
)

REPLACEMENT_1 = (
    f"                # {APPLIED_MARKER_1}: jiter (the Rust JSON parser the\n"
    "                # Anthropic SDK's streaming snapshot builder uses to reassemble\n"
    "                # tool-call input_json_delta chunks) raises a bare ValueError on\n"
    "                # malformed JSON -- e.g. \"expected value at line 1 column 87\" --\n"
    "                # that is NOT a json.JSONDecodeError subclass, so the exclusion\n"
    "                # below never catches it. Message-substring detection since\n"
    "                # jiter's ValueError isn't a distinguishable type. See patch file\n"
    "                # for the full incident writeup.\n"
    "                _JITER_PARSE_ERROR_MARKERS = (\n"
    "                    \"expected value at line\",\n"
    "                    \"expected ident at line\",\n"
    "                    \"eof while parsing\",\n"
    "                    \"trailing characters at line\",\n"
    "                )\n"
    "                _is_jiter_parse_error = (\n"
    "                    isinstance(api_error, ValueError)\n"
    "                    and any(\n"
    "                        _marker in str(api_error).lower()\n"
    "                        for _marker in _JITER_PARSE_ERROR_MARKERS\n"
    "                    )\n"
    "                )\n"
    "                is_local_validation_error = (\n"
    "                    isinstance(api_error, (ValueError, TypeError))\n"
    "                    and not isinstance(\n"
    "                        api_error, (UnicodeEncodeError, json.JSONDecodeError)\n"
    "                    )\n"
    "                    and not _is_jiter_parse_error\n"
)

# --- Sub-patch 2: run_agent.py::_is_provider_stream_parse_error --------

ANCHOR_2 = (
    "        if not isinstance(error, ValueError):\n"
    "            return False\n"
    "        if isinstance(error, (UnicodeEncodeError, json.JSONDecodeError)):\n"
    "            return False\n"
    "        message = str(error).strip().lower()\n"
    "        return \"expected ident at line\" in message\n"
)

REPLACEMENT_2 = (
    "        if not isinstance(error, ValueError):\n"
    "            return False\n"
    "        if isinstance(error, (UnicodeEncodeError, json.JSONDecodeError)):\n"
    "            return False\n"
    f"        # {APPLIED_MARKER_2} of 2): widened from a single hardcoded\n"
    "        # \"expected ident at line\" substring to the full jiter\n"
    "        # parse-failure marker set (kept in sync with\n"
    "        # conversation_loop.py's is_local_validation_error exclusion --\n"
    "        # same root cause, same fix pattern, two call sites). Incident\n"
    "        # 2026-07-22: a real jiter error (\"expected value at line 1\n"
    "        # column 98\") missed the old single-substring check here, fell\n"
    "        # through to the non-retryable stub path, and re-triggered the\n"
    "        # eager-fallback flow this whole patch exists to prevent.\n"
    "        message = str(error).strip().lower()\n"
    "        _JITER_PARSE_ERROR_MARKERS = (\n"
    "            \"expected value at line\",\n"
    "            \"expected ident at line\",\n"
    "            \"eof while parsing\",\n"
    "            \"trailing characters at line\",\n"
    "        )\n"
    "        return any(_marker in message for _marker in _JITER_PARSE_ERROR_MARKERS)\n"
)


def _apply_subpatch(
    *,
    module_name: str,
    applied_marker: str,
    anchor: str,
    replacement: str,
    label: str,
) -> bool:
    """Apply one sub-patch. Returns True if a change was written."""
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise SystemExit(f"patch: cannot locate {module_name}")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if applied_marker in src:
        print(f"patch: {label} already applied to {path} — no-op")
        return False

    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 occurrence of the {label} anchor "
            f"in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(anchor, replacement, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: {label} applied to {path}")
    return True


def main() -> int:
    _apply_subpatch(
        module_name="agent.conversation_loop",
        applied_marker=APPLIED_MARKER_1,
        anchor=ANCHOR_1,
        replacement=REPLACEMENT_1,
        label="is_local_validation_error jiter exclusion (part 1/2)",
    )
    _apply_subpatch(
        module_name="run_agent",
        applied_marker=APPLIED_MARKER_2,
        anchor=ANCHOR_2,
        replacement=REPLACEMENT_2,
        label="_is_provider_stream_parse_error marker widening (part 2/2)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
