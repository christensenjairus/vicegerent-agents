#!/usr/bin/env python3
"""Treat jiter's bare ValueError on malformed streamed tool-call JSON as
retryable, not a local programming bug.

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

So the classifier's existing ``json.JSONDecodeError`` exclusion never
fires for this case. The bare ``ValueError`` is misclassified as
``is_local_validation_error = True`` (a "programming bug"), which
immediately triggers the non-retryable-error path -> fails over to
whatever ``fallback_providers`` entry is configured, instead of a
same-provider retry (the actual fix for a transient stream-decode glitch).

Live incident (2026-07-21, this deployment): a Slack turn's primary
Anthropic call returned HTTP 200 (confirmed via agentgateway's own access
log — 3932ms, 202 output tokens) but Hermes's client raised
``ValueError: expected value at line 1 column 87`` parsing it. The
classifier misrouted this to the ``gpt-5.4`` fallback, which then hard-400'd
on an unrelated ``reasoning_effort``-on-``/chat/completions`` bug (see
sibling patch 0038), leaving the user with no response at all. Both bugs
compound: this patch stops the misroute in the first place; 0038 makes the
fallback survive if it's ever taken again for a different reason.

There's a partial, disconnected acknowledgment of jiter's error shape
already in the codebase: ``run_agent.py``'s ``_summarize_api_error`` special-
cases the substring "expected ident at line" to produce a nicer log message
("Malformed provider streaming response: ..."). That has ZERO effect on the
retry/fallback decision in ``conversation_loop.py`` — it only prettifies
what gets logged after the (wrong) decision was already made. This patch
fixes the actual decision.

Fix
---
Add a jiter-parse-error detector (message-substring based, since jiter's
``ValueError`` isn't a distinguishable subclass) alongside the existing
``json.JSONDecodeError`` exclusion in ``is_local_validation_error``. Markers
cover every jiter parse-failure shape observed in its own error strings
("expected value/ident at line...", "EOF while parsing...", "trailing
characters at line..."). A false-positive match against a genuine local
``ValueError`` unrelated to JSON parsing is essentially impossible given
how specific jiter's phrasing is, and worst case only causes one extra
same-provider retry before the normal retry/fallback machinery still runs.

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream refactored the classifier), the patch raises and
the image build fails, signalling a re-verify. Idempotent: a re-run after
a successful apply is a no-op.

Remove once upstream either (a) makes the classifier check jiter's actual
exception type instead of message substrings, or (b) the Anthropic SDK
wraps jiter parse failures in a dedicated, catchable exception type.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0037"

ANCHOR = (
    "                is_local_validation_error = (\n"
    "                    isinstance(api_error, (ValueError, TypeError))\n"
    "                    and not isinstance(\n"
    "                        api_error, (UnicodeEncodeError, json.JSONDecodeError)\n"
    "                    )\n"
)

REPLACEMENT = (
    f"                # {APPLIED_MARKER}: jiter (the Rust JSON parser the\n"
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


def main() -> int:
    spec = importlib.util.find_spec("agent.conversation_loop")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/conversation_loop.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 occurrence of the is_local_validation_error "
            f"anchor in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: jiter streaming-parse ValueError now treated as retryable "
        f"(not a local programming bug) in {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
