#!/usr/bin/env python3
"""Vicegerent patch: log the actual traceback when a tool check_fn raises.

Context
-------
tools/registry.py's _check_fn_cached() wraps every check_fn call in a bare
`try: ... except Exception: value = False; raised = True`, then logs only
`check_fn %s raised; dependent tools will be unavailable this turn` — the
exception itself (type, message, traceback) is discarded. When a check_fn
genuinely raises (as opposed to cleanly returning False), the one-line
summary gives no way to diagnose root cause; you only learn a check_fn
failed, never why.

Hit live diagnosing a `_check_file_reqs raised` warning in gateway/dashboard
startup logs — check_file_requirements() -> check_terminal_requirements()
already has its own internal try/except that can't raise, so a "raised"
verdict for that check_fn implies the exception happened during the lazy
import chain leading into it (tools/terminal_tool.py module-level imports,
or tools/environments/* backend modules), which the swallowed exception
made impossible to pin down from logs alone.

Fix
---
Add `logger.exception(...)` inside the except block (log level ERROR, full
traceback, only valid inside an except handler) right after `raised = True`.
Purely additive — does not change control flow, caching behavior, or the
existing `raised` bool used by the subsequent flake-vs-real-outage log
lines. Those callers already receive `raised=True` unchanged; this patch
only adds a second, more detailed log line alongside the existing summary.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this function), the patch raises and the image build
fails, signalling a re-verify.

Remove once upstream Hermes logs check_fn exceptions with a traceback
itself.
"""
import importlib.util
import sys

ANCHOR = (
    "    raised = False\n"
    "    try:\n"
    "        value = bool(fn())\n"
    "    except Exception:\n"
    "        value = False\n"
    "        raised = True\n"
)

REPLACEMENT = (
    "    raised = False\n"
    "    try:\n"
    "        value = bool(fn())\n"
    "    except Exception:\n"
    "        value = False\n"
    "        raised = True\n"
    "        # Vicegerent patch 0018: the summary log lines below only ever say\n"
    "        # \"raised\" — log the actual exception + traceback so a check_fn\n"
    "        # failure is diagnosable without re-running it by hand.\n"
    "        logger.exception(\n"
    "            \"check_fn %s raised an exception\",\n"
    "            getattr(fn, \"__qualname__\", fn),\n"
    "        )\n"
)

APPLIED_MARKER = "Vicegerent patch 0018"


def main() -> int:
    spec = importlib.util.find_spec("tools.registry")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/registry.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 _check_fn_cached except-block anchor in "
            f"{path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: check_fn exceptions now logged with traceback in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
