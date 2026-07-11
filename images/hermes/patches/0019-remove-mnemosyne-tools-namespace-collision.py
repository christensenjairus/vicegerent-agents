#!/usr/bin/env python3
"""Vicegerent patch: remove mnemosyne-memory's stray top-level `tools/` dir.

Context
-------
mnemosyne-memory==3.11.1 (pulled in transitively by mnemosyne-hermes[all] in
requirements.txt) ships a wheel that installs its own internal dev/benchmark
scripts (bench_100k_gemma.py, evaluate_beam_end_to_end.py, diagnostic_tr.py,
etc. -- upstream project scratch scripts, not a real importable package)
under a bare top-level directory named `tools/` in site-packages, with no
`__init__.py`.

Because that directory has no `__init__.py`, Python treats it as an
*implicit namespace package* portion for the name `tools`. Hermes Agent
itself ships a real `tools` package (the editable install at
/opt/hermes/tools, exposed via the `__editable__.hermes_agent` finder's
explicit MAPPING). Python's import resolution order means the ordinary
`PathFinder` (which finds the mnemosyne-memory namespace-package portion in
site-packages) runs *before* the editable-install `_EditableFinder` in
sys.meta_path for any process whose cwd doesn't itself contain a `tools/`
subdirectory -- which is every real Hermes session (terminal.cwd:
/workspace in config.yaml). The result: `import tools` silently resolves
to the bogus mnemosyne-memory namespace package instead of Hermes's real
one.

This breaks any check_fn (or other code) that does a delayed `from tools
import X` from inside a submodule, most visibly
tools/file_tools.py::_check_file_reqs, which does `from tools import
check_file_requirements` to avoid a circular import. That function doesn't
exist on the bogus namespace package, so the import raises ImportError,
the file toolset's check_fn is treated as failed, and `hermes doctor`
reports "file (system dependency not met)" even though read_file/write_file/
patch/search_files work fine when the real tools/__init__.py is reached
(e.g. cwd == /opt/hermes). Confirmed live: `cd /workspace && python3 -c
"import tools; print(tools.__file__)"` -> None (namespace package);
`cd /opt/hermes && python3 -c "import tools; print(tools.__file__)"` ->
/opt/hermes/tools/__init__.py. Also visible as repeated
`ImportError: cannot import name 'check_file_requirements' from 'tools'
(unknown location)` tracebacks in ~/.hermes/logs/errors.log.

Fix
---
Delete the stray site-packages/tools/ directory entirely. It is not
imported by mnemosyne-memory's own package code (mnemosyne/, not tools/,
is mnemosyne-memory's real importable namespace) -- these are upstream
project maintenance/benchmark scripts that were never meant to ship in the
wheel and are not referenced by mnemosyne_hermes or mnemosyne_memory at
runtime. Fail loud if the directory doesn't look like this known bogus
package (no __init__.py) or is missing -- either means the mnemosyne-memory
release changed and this patch needs re-verification.

Remove once mnemosyne-memory stops shipping tools/ as a floating top-level
directory in its wheel (upstream: https://github.com/AxDSan/mnemosyne).
"""
import shutil
import sys
from pathlib import Path


def main() -> int:
    site_packages = Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    stray_tools = site_packages / "tools"

    if not stray_tools.is_dir():
        print(f"patch: {stray_tools} not present — already removed or never installed, no-op")
        return 0

    if (stray_tools / "__init__.py").exists():
        raise SystemExit(
            f"patch: {stray_tools} has an __init__.py — this is no longer the "
            "bogus mnemosyne-memory namespace-package directory. Re-verify "
            "before deleting (upstream tools/ layout may have changed, or "
            "this could be a real package)."
        )

    # Sanity check: confirm this is actually the known mnemosyne-memory
    # scratch-script dump, not something else that happens to be named
    # `tools/` with no __init__.py.
    known_marker = stray_tools / "evaluate_beam_end_to_end.py"
    if not known_marker.exists():
        raise SystemExit(
            f"patch: {stray_tools} exists but is missing the expected "
            f"{known_marker.name} marker file — contents don't match the "
            "known mnemosyne-memory==3.11.1 stray tools/ dump. Re-verify "
            "before deleting."
        )

    shutil.rmtree(stray_tools)
    print(f"patch: removed stray namespace-package directory {stray_tools}")

    # Verify the real Hermes `tools` package now resolves cleanly and its
    # file-toolset check_fn no longer raises.
    import importlib
    import tools as _tools
    importlib.reload(_tools)
    assert _tools.__file__ and _tools.__file__.endswith("tools/__init__.py"), (
        f"patch: 'tools' still resolves to {_tools.__file__!r} after removal"
    )
    from tools import check_file_requirements  # noqa: F401
    print(f"patch: verified 'tools' now resolves to {_tools.__file__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
