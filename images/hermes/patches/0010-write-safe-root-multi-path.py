#!/usr/bin/env python3
"""Vicegerent patch: support multiple HERMES_WRITE_SAFE_ROOT paths.

Context
-------
agent/file_safety.py::get_safe_write_root() treats the whole
HERMES_WRITE_SAFE_ROOT value as a single literal path -- os.path.realpath()
on a colon-delimited value like "/opt/data:/workspace" (the PATH-style
convention this repo's own sandbox.yaml sets, so the agent can write both
its data dir and repo checkouts under /workspace) returns that string
verbatim. No real file on disk can ever equal or start with it, so
is_write_denied() denies every write_file/patch call regardless of target,
surfaced only as a generic "protected system/credential file" error with
no signal that the actual cause is a malformed safe-root value.

This patch adds get_safe_write_roots() (plural), splitting on os.pathsep
and skipping blank segments, and updates is_write_denied() to allow a path
under ANY configured root. get_safe_write_root() (singular) is kept as a
compatibility shim returning the first root.

Upstream fix filed against nousresearch/hermes-agent. Remove this patch
once it (or an equivalent fix) lands in the pinned base image.

Fail-loud by design: if the upstream anchor is absent or appears more than
once (i.e. upstream refactored this path -- possibly landing the real fix),
the patch raises and the image build fails, signalling a re-verify.
"""
import importlib.util
import sys

ANCHOR = (
    'def get_safe_write_root() -> Optional[str]:\n'
    '    """Return the resolved HERMES_WRITE_SAFE_ROOT path, or None if unset."""\n'
    '    root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")\n'
    '    if not root:\n'
    '        return None\n'
    '    try:\n'
    '        return os.path.realpath(os.path.expanduser(root))\n'
    '    except Exception:\n'
    '        return None\n'
)

REPLACEMENT = (
    'def get_safe_write_roots() -> list[str]:\n'
    '    """Return the resolved HERMES_WRITE_SAFE_ROOT paths, or [] if unset.\n'
    '\n'
    '    Supports os.pathsep-delimited multiple roots (":" on POSIX, ";" on\n'
    '    Windows), matching the convention of PATH-like env vars. Blank\n'
    '    segments (from a leading/trailing/doubled separator) are skipped\n'
    '    rather than resolving to a bogus root that would deny everything.\n'
    '    A single unset or empty var still means "no restriction".\n'
    '\n'
    '    Vicegerent patch for the HERMES_WRITE_SAFE_ROOT multi-path fix.\n'
    '    """\n'
    '    raw = os.getenv("HERMES_WRITE_SAFE_ROOT", "")\n'
    '    if not raw:\n'
    '        return []\n'
    '    roots: list[str] = []\n'
    '    for segment in raw.split(os.pathsep):\n'
    '        segment = segment.strip()\n'
    '        if not segment:\n'
    '            continue\n'
    '        try:\n'
    '            roots.append(os.path.realpath(os.path.expanduser(segment)))\n'
    '        except Exception:\n'
    '            continue\n'
    '    return roots\n'
    '\n'
    '\n'
    'def get_safe_write_root() -> Optional[str]:\n'
    '    """Deprecated: use get_safe_write_roots(). Kept for compatibility;\n'
    '    returns the first configured root, or None if unset."""\n'
    '    roots = get_safe_write_roots()\n'
    '    return roots[0] if roots else None\n'
)

DENY_ANCHOR = (
    "    safe_root = get_safe_write_root()\n"
    "    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):\n"
    "        return True\n"
)

DENY_REPLACEMENT = (
    "    safe_roots = get_safe_write_roots()\n"
    "    if safe_roots and not any(\n"
    "        resolved == root or resolved.startswith(root + os.sep) for root in safe_roots\n"
    "    ):\n"
    "        return True\n"
)

# Idempotence marker: a unique token from the replacement so a re-run is a
# no-op rather than a hard failure (the anchor is gone after the first apply).
APPLIED_MARKER = "def get_safe_write_roots() -> list[str]:"


def main() -> int:
    spec = importlib.util.find_spec("agent.file_safety")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agent/file_safety.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 get_safe_write_root anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify the write-safe-root "
            "path, the real multi-path fix may have landed)"
        )
    deny_count = src.count(DENY_ANCHOR)
    if deny_count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 is_write_denied safe-root check in "
            f"{path}, found {deny_count} (upstream drifted -- re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT)
    src = src.replace(DENY_ANCHOR, DENY_REPLACEMENT)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied write-safe-root multi-path fix to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
