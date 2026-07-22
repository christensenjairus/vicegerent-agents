#!/usr/bin/env python3
"""Behavioral regression test for patch 0037 (jiter parse error retryable).

Covers BOTH classifiers patch 0037 touches — they are the same root cause
(jiter's bare ValueError on malformed streamed tool-call JSON) at two
independent call sites, and both must agree on what counts as retryable:

1. ``agent/conversation_loop.py::is_local_validation_error`` — gates the
   post-retry-exhaustion fallback decision.
2. ``run_agent.py::AIAgent._is_provider_stream_parse_error`` — gates the
   mid-stream silent-retry decision (tool call in-flight when the stream
   dies). Incident 2026-07-22: this second classifier only recognized
   "expected ident at line" and missed "expected value at line" (the shape
   that actually fired), falling through to the eager-fallback path this
   whole patch exists to prevent.

Usage: run this INSIDE a Hermes image/container after the patch has been
applied (i.e. against the live installed files), or against scratch copies
during patch development.

    HERMES_CONVERSATION_LOOP=/path/to/conversation_loop.py \\
    HERMES_RUN_AGENT=/path/to/run_agent.py \\
    python3 test_0037_jiter_parse_retryable.py

Test 1 exercises the exact `is_local_validation_error` classifier
expression by extracting it out of the live module source (never
hand-copied — always re-reads the shipped file so this test fails loudly
if the patch's anchor ever drifts). Test 2 imports the real
`_is_provider_stream_parse_error` method directly via importlib and calls
it, since that classifier is a normal bound method (no snippet extraction
needed — it doesn't reference enclosing-scope locals the way the inline
conversation_loop.py block does).

Exit code 0 = all assertions passed. Any failure raises AssertionError with
a descriptive message and exits non-zero.
"""
from __future__ import annotations

import importlib.util
import json
import os
import ssl
import sys


def _load_classifier_snippet(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    start_marker = "_JITER_PARSE_ERROR_MARKERS = ("
    end_marker = "is_client_error = ("
    if start_marker not in src:
        raise AssertionError(
            f"patch 0037 anchor '_JITER_PARSE_ERROR_MARKERS' not found in {path} "
            "-- patch was not applied, or upstream refactored the classifier"
        )
    if end_marker not in src:
        raise AssertionError(
            f"end marker 'is_client_error = (' not found in {path} -- "
            "upstream refactored the classifier downstream of our patch"
        )
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    if end <= start:
        raise AssertionError("end marker appears before start marker -- unexpected file shape")
    return src[start:end]


def _evaluate_classifier(snippet: str, api_error: Exception) -> bool:
    """Run the extracted classifier snippet in a minimal namespace and
    return the resulting `is_local_validation_error` bool."""
    import textwrap

    ns = {"api_error": api_error, "json": json, "ssl": ssl}
    lines = snippet.split("\n")
    if lines:
        # The extraction boundary strips leading whitespace only from the
        # very first line (the start-marker match begins mid-indent);
        # every other line keeps its original 16-space indentation.
        # Restore it before dedenting so textwrap.dedent has a consistent
        # common prefix to strip.
        first_indent = " " * 16
        lines[0] = first_indent + lines[0]
    dedented = textwrap.dedent("\n".join(lines))
    code = "if True:\n" + textwrap.indent(dedented, "    ")
    exec(compile(code, "<classifier-snippet>", "exec"), ns)
    return ns["is_local_validation_error"]


def _test_conversation_loop_classifier() -> tuple[int, list[str]]:
    """Test 1: agent/conversation_loop.py::is_local_validation_error."""
    path = os.environ.get(
        "HERMES_CONVERSATION_LOOP",
        "/opt/hermes/agent/conversation_loop.py",
    )
    snippet = _load_classifier_snippet(path)

    cases: list[tuple[str, Exception, bool]] = []

    # --- The exact live-incident shape: jiter's bare ValueError ---
    cases.append((
        "jiter 'expected value at line N column N' (live incident shape)",
        ValueError("expected value at line 1 column 87"),
        False,  # must NOT be classified as local-validation (must be retryable)
    ))
    cases.append((
        "jiter 'expected ident at line N column N'",
        ValueError("expected ident at line 1 column 2"),
        False,
    ))
    cases.append((
        "jiter 'EOF while parsing a value at line N column N'",
        ValueError("EOF while parsing a value at line 1 column 0"),
        False,
    ))
    cases.append((
        "jiter 'trailing characters at line N column N'",
        ValueError("trailing characters at line 1 column 95"),
        False,
    ))

    # --- Real jiter, if importable, for maximum fidelity ---
    try:
        import jiter  # type: ignore

        try:
            jiter.from_json(b"not json at all, well past column eighty seven characters for sure")
        except ValueError as real_jiter_exc:
            cases.append((
                f"REAL jiter.from_json() ValueError: {real_jiter_exc}",
                real_jiter_exc,
                False,
            ))
    except ImportError:
        print("note: jiter not importable in this environment; skipping real-jiter case")

    # --- Must NOT regress: genuine json.JSONDecodeError still excluded ---
    try:
        json.loads("not json")
    except json.JSONDecodeError as jde:
        cases.append((
            "json.JSONDecodeError (pre-existing exclusion, must still work)",
            jde,
            False,
        ))

    # --- Must NOT regress: UnicodeEncodeError still excluded ---
    try:
        "\ud800".encode("utf-8")
    except UnicodeEncodeError as uee:
        cases.append((
            "UnicodeEncodeError (pre-existing exclusion, must still work)",
            uee,
            False,
        ))

    # --- Must NOT regress: ssl.SSLError still excluded ---
    cases.append((
        "ssl.SSLError (pre-existing exclusion, must still work)",
        ssl.SSLError("some tls failure"),
        False,
    ))

    # --- Must NOT regress: NoneType-not-iterable TypeError still excluded ---
    cases.append((
        "TypeError 'NoneType is not iterable' (pre-existing exclusion)",
        TypeError("'NoneType' object is not iterable"),
        False,
    ))

    # --- A genuine local programming-bug ValueError must STILL be classified
    #     as a local validation error (i.e. our new exclusion must be narrow) ---
    cases.append((
        "genuine unrelated ValueError (must remain non-retryable/local)",
        ValueError("invalid literal for int() with base 10: 'abc'"),
        True,
    ))
    cases.append((
        "genuine unrelated TypeError (must remain non-retryable/local)",
        TypeError("unsupported operand type(s) for +: 'int' and 'str'"),
        True,
    ))

    failures = []
    print(f"\n--- Test 1: {path}::is_local_validation_error ---")
    for label, exc, expected in cases:
        actual = _evaluate_classifier(snippet, exc)
        status = "PASS" if actual == expected else "FAIL"
        print(f"[{status}] {label}: is_local_validation_error={actual} (expected {expected})")
        if actual != expected:
            failures.append(label)

    return len(cases), failures


def _test_stream_parse_error_classifier() -> tuple[int, list[str]]:
    """Test 2: run_agent.py::AIAgent._is_provider_stream_parse_error.

    This is a normal bound method (no enclosing-scope locals), so it's
    imported and called directly via importlib rather than snippet-extracted.
    """
    path = os.environ.get("HERMES_RUN_AGENT", "/opt/hermes/run_agent.py")

    spec = importlib.util.spec_from_file_location("run_agent_under_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        raise AssertionError(
            f"failed to import {path} — is /opt/hermes on sys.path? {exc}"
        )

    method = module.AIAgent._is_provider_stream_parse_error

    class _FakeAgent:
        api_mode = "anthropic_messages"

    fake = _FakeAgent()

    cases: list[tuple[str, Exception, bool]] = [
        (
            "jiter 'expected value at line N column N' "
            "(2026-07-22 incident shape — previously MISSED by this classifier)",
            ValueError("expected value at line 1 column 98"),
            True,
        ),
        (
            "jiter 'expected ident at line N column N' (original 0037 shape)",
            ValueError("expected ident at line 1 column 149"),
            True,
        ),
        (
            "jiter 'EOF while parsing a value at line N column N'",
            ValueError("EOF while parsing a value at line 1 column 0"),
            True,
        ),
        (
            "jiter 'trailing characters at line N column N'",
            ValueError("trailing characters at line 1 column 95"),
            True,
        ),
        (
            "json.JSONDecodeError (pre-existing exclusion, must still work)",
            None,  # filled in below
            False,
        ),
        (
            "genuine unrelated ValueError (must remain False -- not a jiter error)",
            ValueError("invalid literal for int() with base 10: 'abc'"),
            False,
        ),
        (
            "non-anthropic api_mode (function must short-circuit False regardless of message)",
            ValueError("expected value at line 1 column 98"),
            False,
        ),
    ]

    try:
        json.loads("not json")
    except json.JSONDecodeError as jde:
        cases[4] = (cases[4][0], jde, False)

    failures = []
    print(f"\n--- Test 2: {path}::AIAgent._is_provider_stream_parse_error ---")
    for label, exc, expected in cases:
        if "non-anthropic api_mode" in label:
            other_fake = _FakeAgent()
            other_fake.api_mode = "chat_completions"
            actual = method(other_fake, exc)
        else:
            actual = method(fake, exc)
        status = "PASS" if actual == expected else "FAIL"
        print(f"[{status}] {label}: result={actual} (expected {expected})")
        if actual != expected:
            failures.append(label)

    return len(cases), failures


def main() -> int:
    total_cases = 0
    all_failures: list[str] = []

    count1, failures1 = _test_conversation_loop_classifier()
    total_cases += count1
    all_failures.extend(failures1)

    count2, failures2 = _test_stream_parse_error_classifier()
    total_cases += count2
    all_failures.extend(failures2)

    if all_failures:
        raise AssertionError(f"{len(all_failures)} case(s) failed: {all_failures}")

    print(f"\nAll {total_cases} cases passed across both classifiers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
