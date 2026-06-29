#!/usr/bin/env python3
"""Vicegerent patch: make agentburn hermes adapter honour HERMES_HOME env var.

In the vicegerent sandbox, Hermes stores state.db at $HERMES_HOME/state.db
(e.g. /opt/data/state.db), not ~/.hermes/state.db. The agentburn hermes
adapter hardcodes the path as os.path.join(expanduser("~"), ".hermes",
"state.db"), so the MCP server can't find the DB and every burn_report call
fails with FileNotFoundError.

This patch makes default_db_path() check HERMES_HOME first, matching how
Hermes itself resolves its home directory.

Remove once agentburn upstreams HERMES_HOME support.
"""
import importlib.util
import sys

ANCHOR = 'def default_db_path() -> str:\n    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'

REPLACEMENT = (
    'def default_db_path() -> str:\n'
    '    _home = os.environ.get("HERMES_HOME", "").strip()\n'
    '    if _home:\n'
    '        return os.path.join(_home, "state.db")\n'
    '    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'
)

APPLIED_MARKER = 'HERMES_HOME", "").strip()'


def main() -> int:
    spec = importlib.util.find_spec("agentburn.adapters.hermes")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn.adapters.hermes module")
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
            "(agentburn upstream changed adapters/hermes.py — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: HERMES_HOME support added to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
