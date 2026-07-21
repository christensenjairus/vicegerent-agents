#!/usr/bin/env python3
"""Behavioral regression test for patch 0037 (jiter parse error retryable).

Usage: run this INSIDE a Hermes image/container after the patch has been
applied (i.e. against the live installed agent/conversation_loop.py), or
against a scratch copy during patch development.

    HERMES_CONVERSATION_LOOP=/path/to/conversation_loop.py python3 test_0037.py

Exercises the exact `is_local_validation_error` classifier expression by
extracting it out of the live module source (never hand-copied — always
re-reads the shipped file so this test fails loudly if the patch's anchor
ever drifts) and evaluating it against a matrix of real exception instances,
including actual jiter errors when the jiter package is importable.

Exit code 0 = all assertions passed. Any failure raises AssertionError with
a descriptive message and exits non-zero.
"""
from __future__ import annotations

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


def main() -> int:
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
    for label, exc, expected in cases:
        actual = _evaluate_classifier(snippet, exc)
        status = "PASS" if actual == expected else "FAIL"
        print(f"[{status}] {label}: is_local_validation_error={actual} (expected {expected})")
        if actual != expected:
            failures.append(label)

    if failures:
        raise AssertionError(f"{len(failures)} case(s) failed: {failures}")

    print(f"\nAll {len(cases)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
