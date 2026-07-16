#!/usr/bin/env python3
"""Vicegerent patch: fix tools/skill_manager_tool.py's background-curator
read-before-write tracking so it survives concurrent tool dispatch.

Context / investigation
------------------------
/opt/data/logs/errors.log shows 223 occurrences since 2026-07-05 of the
background curator's skill_manage calls failing -- 100% of its patch/edit
attempts across 10+ days and dozens of skills -- with:

    Refusing background curator patch for skill '<name>': the current
    SKILL.md content has not been loaded in this review turn. Call
    skill_view(name) for SKILL.md

The curator DOES call skill_view(name) before skill_manage(action='patch')
in the same review turn -- this is not a missing-call / prompt bug. The
guard's own bookkeeping is broken by a ContextVar/thread-pool interaction:

1. ``agent/background_review.py`` runs the curator's forked review agent
   (``review_agent.run_conversation(...)``) on one dedicated ``bg-review``
   thread (started via ``threading.Thread(target=propagate_context_to_thread
   (target), ...)`` in ``run_agent.py::_spawn_background_review``).

2. Inside that run, ``agent/tool_executor.py`` dispatches a batch of tool
   calls. ``skill_view`` is listed in ``agent/tool_dispatch_helpers.py``'s
   ``_PARALLEL_SAFE_TOOLS``, so a batch containing multiple ``skill_view``
   calls (or ``skill_view`` alongside other parallel-safe tools -- exactly
   what an LLM naturally emits during a multi-skill consolidation sweep)
   gets dispatched via ``execute_tool_calls_concurrent``, which submits each
   call to a ``DaemonThreadPoolExecutor`` worker via
   ``tools.thread_context.propagate_context_to_thread``. That helper calls
   ``contextvars.copy_context()`` *once per submission, on the parent
   thread*, and runs the worker inside ``ctx.run(_inner)``.

3. ``tools/skill_manager_tool.py``'s read-mark bookkeeping is a
   ``contextvars.ContextVar[frozenset[str]]``
   (``_background_review_read_paths``). ``mark_background_review_skill_read``
   marks a path read by doing ``current = set(cv.get()); current.add(x);
   cv.set(frozenset(current))`` -- i.e. it *rebinds* a new frozenset on the
   ContextVar. A ``ContextVar.set()`` inside a ``copy_context()``-derived
   Context only mutates *that copy*; it is never visible outside it, not
   even back on the parent thread that spawned the copy. So when
   ``skill_view`` for skill X runs in worker-thread-copy A and calls
   ``mark_background_review_skill_read``, that mark is trapped inside
   A's private copied context and discarded when A's ``ctx.run()`` returns.
   The later ``skill_manage(action='patch')`` call for the same skill --
   whether in the same batch (its own separate copied context) or a later
   sequential turn -- reads the *original* (still-empty) ContextVar binding
   and finds nothing marked. ``_background_review_read_before_write_guard``
   therefore fires unconditionally, every time, for every background-review
   patch/edit/delete/write_file/remove_file call. Confirmed empirically: a
   ContextVar rebind (``cv.set(new_frozenset)``) made inside a
   ``copy_context()`` copy is invisible to sibling copies AND to the parent
   thread; a ContextVar bound ONCE to a shared *mutable* container (whose
   contents are then mutated in place rather than rebound) IS visible
   across sibling copies, because ``copy_context()`` copies the variable
   *binding* (a reference to the same object), not a deep copy of the
   referenced object.

Fix
---
Change ``_background_review_read_paths`` from holding an immutable
``frozenset`` (rebind-per-mark, isolated per Context copy) to holding a
plain mutable ``set`` that is bound ONCE per curator run (in
``_reset_background_review_read_marks()``, already called by
``agent/background_review.py`` before the review agent's
``run_conversation()`` -- i.e. before any tool-call forking happens) and
mutated *in place* thereafter via ``.add()``. Because ``copy_context()``
copies the ContextVar *binding* (a reference), every worker-thread copy
forked after that one-time bind shares the exact same underlying ``set``
object, so a mark made in one worker's copied context is visible when any
other copy (or the parent thread) reads that same ContextVar.

Only three call sites change, all inside this module:

  * The ContextVar's default and type annotation (``frozenset`` -> ``set``,
    default becomes ``None`` since a mutable default object must not be
    shared across the ContextVar's *own* un-reset default binding --
    ``.get()`` is only ever called after ``_reset_background_review_read_marks``
    has bound a fresh set for the run, mirroring the pre-patch code's
    reliance on that reset call).
  * ``mark_background_review_skill_read``: mutate in place (``.add()``)
    instead of read-copy-rebind.
  * ``_background_review_has_read``: unchanged logic, just reads through
    the (now mutable) container -- included in the anchor for a clean
    contiguous diff, not because its behavior changes.
  * ``_reset_background_review_read_marks``: bind a fresh ``set()`` instead
    of ``frozenset()``.

No change to when/why the guard fires, no change to any public tool
signature or return shape -- purely fixes the underlying bookkeeping so the
guard's *true negative* case (curator actually read the file) is reachable
at all. The guard's true positive case (curator never read the file) is
unaffected and still fires correctly.

Fail-loud by design: if the anchor is absent or appears more than once
(upstream refactored this bookkeeping), the patch raises and the image
build fails, signalling a re-verify. Idempotent: a re-run after a
successful apply is a no-op.

Remove once upstream fixes this ContextVar/copy_context interaction
(tracked informally here; no upstream issue number filed yet as of
2026-07-16).
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0025"

ANCHOR = (
    "_background_review_read_paths: \"_ctxvars.ContextVar[frozenset[str]]\" = _ctxvars.ContextVar(\n"
    "    \"background_review_read_paths\", default=frozenset()\n"
    ")\n"
    "\n"
    "\n"
    "def mark_background_review_skill_read(path: Path) -> None:\n"
    "    \"\"\"Record that the active background-review fork has read a skill file.\n"
    "\n"
    "    The autonomous review fork is allowed to evolve skills, but it must not\n"
    "    patch or rewrite content it has only inferred from the transcript.  The\n"
    "    skill_view tool calls this after returning file content to the model; write\n"
    "    paths below require the corresponding target path to be present when the\n"
    "    current origin is ``background_review``.\n"
    "    \"\"\"\n"
    "    try:\n"
    "        from tools.skill_provenance import is_background_review\n"
    "        if not is_background_review():\n"
    "            return\n"
    "    except Exception:\n"
    "        return\n"
    "\n"
    "    try:\n"
    "        resolved = str(path.resolve())\n"
    "    except Exception:\n"
    "        resolved = str(path)\n"
    "    current = set(_background_review_read_paths.get())\n"
    "    current.add(resolved)\n"
    "    _background_review_read_paths.set(frozenset(current))\n"
    "\n"
    "\n"
    "def _background_review_has_read(path: Path) -> bool:\n"
    "    try:\n"
    "        resolved = str(path.resolve())\n"
    "    except Exception:\n"
    "        resolved = str(path)\n"
    "    return resolved in _background_review_read_paths.get()\n"
    "\n"
    "\n"
    "def _reset_background_review_read_marks() -> None:\n"
    "    \"\"\"Test helper: clear read-before-write marks for the current context.\"\"\"\n"
    "    _background_review_read_paths.set(frozenset())\n"
)

REPLACEMENT = (
    "# Vicegerent patch 0025: a plain mutable set bound ONCE per curator run,\n"
    "# not a frozenset rebound on every mark. ``execute_tool_calls_concurrent``\n"
    "# dispatches parallel-safe tools (skill_view included) onto worker threads\n"
    "# via ``propagate_context_to_thread``, which calls ``contextvars.copy_context()``\n"
    "# per submission. A ContextVar *rebind* (``.set(new_value)``) inside one of\n"
    "# those copies is invisible to sibling copies and to the parent thread --\n"
    "# so the old frozenset-rebind design meant a skill_view mark made in one\n"
    "# worker's copied context could never be seen by the skill_manage(patch)\n"
    "# call that follows, causing the background-curator read-before-write\n"
    "# guard to fire on every single call (confirmed live: 223/223 failures\n"
    "# over 10+ days). A ContextVar bound once to a shared *mutable* container,\n"
    "# then mutated in place, stays visible across every copy forked after that\n"
    "# one bind, because copy_context() copies the variable binding (a\n"
    "# reference), not a deep copy of the referenced object.\n"
    "_background_review_read_paths: \"_ctxvars.ContextVar[Optional[set]]\" = _ctxvars.ContextVar(\n"
    "    \"background_review_read_paths\", default=None\n"
    ")\n"
    "\n"
    "\n"
    "def mark_background_review_skill_read(path: Path) -> None:\n"
    "    \"\"\"Record that the active background-review fork has read a skill file.\n"
    "\n"
    "    The autonomous review fork is allowed to evolve skills, but it must not\n"
    "    patch or rewrite content it has only inferred from the transcript.  The\n"
    "    skill_view tool calls this after returning file content to the model; write\n"
    "    paths below require the corresponding target path to be present when the\n"
    "    current origin is ``background_review``.\n"
    "    \"\"\"\n"
    "    try:\n"
    "        from tools.skill_provenance import is_background_review\n"
    "        if not is_background_review():\n"
    "            return\n"
    "    except Exception:\n"
    "        return\n"
    "\n"
    "    try:\n"
    "        resolved = str(path.resolve())\n"
    "    except Exception:\n"
    "        resolved = str(path)\n"
    "    # Vicegerent patch 0025: mutate the shared set in place. Do NOT read-\n"
    "    # copy-rebind (that recreates the frozenset-isolation bug this patch\n"
    "    # fixes) -- .get() may return the same set object across every\n"
    "    # copy_context() fork made after _reset_background_review_read_marks()\n"
    "    # bound it, so .add() here is what makes the mark visible to them all.\n"
    "    marks = _background_review_read_paths.get()\n"
    "    if marks is None:\n"
    "        marks = set()\n"
    "        _background_review_read_paths.set(marks)\n"
    "    marks.add(resolved)\n"
    "\n"
    "\n"
    "def _background_review_has_read(path: Path) -> bool:\n"
    "    try:\n"
    "        resolved = str(path.resolve())\n"
    "    except Exception:\n"
    "        resolved = str(path)\n"
    "    marks = _background_review_read_paths.get()\n"
    "    return marks is not None and resolved in marks\n"
    "\n"
    "\n"
    "def _reset_background_review_read_marks() -> None:\n"
    "    \"\"\"Bind a fresh, shared mutable read-marks set for this curator run.\n"
    "\n"
    "    Vicegerent patch 0025: called once per run (see\n"
    "    agent/background_review.py) BEFORE the review agent's\n"
    "    run_conversation() -- i.e. before any tool-call batch can fork worker\n"
    "    threads via copy_context(). Every fork made after this bind shares\n"
    "    this exact set object by reference, so marks made in one worker are\n"
    "    visible to all the others and to the parent bg-review thread.\n"
    "    \"\"\"\n"
    "    _background_review_read_paths.set(set())\n"
)


def main() -> int:
    spec = importlib.util.find_spec("tools.skill_manager_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate tools/skill_manager_tool.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 ANCHOR (background-review read-marks "
            f"bookkeeping) in {path}, found {count} (upstream drifted — "
            "re-verify the ContextVar/mark/has_read/reset block)"
        )

    if "from typing import" in src and "Optional" not in src.split("\n\n", 1)[0]:
        # Best-effort sanity check only; the module already imports Optional
        # for its own type hints elsewhere (verified at authoring time), so
        # this is not treated as a hard precondition -- just documented here
        # in case a future upstream refactor drops that import.
        pass

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: background-curator read-marks ContextVar isolation fixed in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
