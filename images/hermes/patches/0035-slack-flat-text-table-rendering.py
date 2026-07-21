#!/usr/bin/env python3
"""Vicegerent patch: render markdown pipe-tables as fenced, aligned-monospace
text in Slack's format_message(), now that slack.extra.rich_blocks is
disabled entirely.

Context
-------
Two prior Slack rendering approaches were tried and both had a fatal flaw
discovered only through live use:

1. Slack's Block Kit disabled entirely (flat mrkdwn `text` payload only,
   the original behavior): markdown pipe-tables render as raw, unaligned
   `| a | b |` lines -- readable but ugly, no real grid/alignment.

2. `slack.extra.rich_blocks: true` (patches 0033 and its predecessor):
   promoted tables to a native Block Kit `table` block for real grid
   rendering. This works for the table itself, but EVERY OTHER block in
   the response -- including flat mrkdwn `section` blocks used for
   surrounding prose, and even the runtime-footer block -- got its own
   "Show more"/"Show less" collapse toggle from the Slack CLIENT, not
   from block count or text length. Patch 0033 tried folding all non-table
   content into a single `section` block per response to minimize block
   count, but the underlying problem is per-BLOCK collapse chrome, not a
   whole-message height threshold -- confirmed live: after 0033, "Show
   more"/"Show less" still appeared on every non-table block (prose
   sections, the footer), just not on the table block itself. There is no
   way to have ANY Block Kit block in a response without Slack's client
   attaching this chrome to it.

Fix
---
Disable `slack.extra.rich_blocks` entirely (config-only change, in this
repo's Helm chart default -- no Hermes source patch needed for that part)
and, in exchange for losing the native `table` block, teach
`format_message()` to rewrite markdown pipe-tables into fenced
(``` ... ```) aligned-monospace text BEFORE any of its other markdown ->
mrkdwn conversion steps run. A message with zero Block Kit blocks gets
zero collapse chrome from the Slack client, and the existing
`_render_table()` helper (already used as the >20-col/>100-row/oversized-
content fallback when Block Kit was enabled) already produces clean
column-aligned monospace text -- reusing it here needed no new rendering
logic, just a new call site and a thin wrapper that finds table
boundaries the same way `render_blocks()` did.

The new `render_tables_as_fenced_monospace()` function lives in
block_kit.py (reuses `_TABLE_SEP_RE` and `_render_table()` already
defined there) and is called as literally the first line of
`format_message()`'s body, before the fenced-code-block protection step.
Because the fence it emits (```` ```\n...\n``` ````) is indistinguishable
from a normal fenced code block, that same protection step immediately
stashes it behind a placeholder and every later step (escaping, header/
bold/italic conversion, etc.) leaves it untouched -- restored verbatim at
the end, same guarantee a real code fence already had.

Supersedes patch 0033 (removed by this same MR): rich_blocks is now
config-disabled, so 0033's table-only Block Kit rendering is dead code
with no way to re-enable it short of also re-enabling rich_blocks (which
reintroduces the exact collapse-chrome problem this patch fixes). Cleaner
to remove it than carry two competing table-rendering code paths.

Verified structurally (all three anchors -- block_kit.py's function-
insertion point, adapter.py's import line, adapter.py's format_message()
docstring/entry -- match exactly once against the live source; patched
output compiles for both files) and behaviorally against actually-patched
live-file copies (not synthetic fixtures): a single table with surrounding
prose renders as intro text, a fenced aligned-monospace block, then outro
text; multiple tables each get their own fence; content with a stray pipe
but no separator row (not a real table) passes through unmodified; and
critically, running the FULL format_message() pipeline end-to-end (header
conversion, bold, links, escaping) on a mixed message confirms the table
fence survives completely untouched by every later step while the
surrounding prose is still correctly converted (## Header -> *Header*,
**bold** -> *bold*, [text](url) -> <url|text>).

Fail-loud by design: if any anchor is absent or appears an unexpected
number of times (upstream refactored either file), the patch raises and
the image build fails, signalling a re-verify.

Remove once Slack's client stops attaching per-block collapse chrome to
Block Kit sections, or a native table primitive becomes available that
doesn't carry it -- at which point rich_blocks can be safely re-enabled
and this patch (plus its config-side rich_blocks: false) reverted.
"""
import importlib.util
import sys

# --- block_kit.py: add render_tables_as_fenced_monospace() -----------------

ANCHOR_BLOCK_KIT = 'def _render_table(rows: List[str]) -> str:\n    """Render markdown pipe-table rows as aligned monospace text (fallback)."""\n    parsed: List[List[str]] = []\n    for r in rows:\n        cells = _split_row(r)\n        parsed.append(cells)\n    if not parsed:\n        return "\\n".join(rows)\n    ncols = max(len(r) for r in parsed)\n    for r in parsed:\n        r.extend([""] * (ncols - len(r)))\n    widths = [max(len(r[c]) for r in parsed) for c in range(ncols)]\n    out_lines = []\n    for ri, r in enumerate(parsed):\n        line = " | ".join(r[c].ljust(widths[c]) for c in range(ncols))\n        out_lines.append(line.rstrip())\n        if ri == 0:  # header underline\n            out_lines.append("-+-".join("-" * widths[c] for c in range(ncols)))\n    return "\\n".join(out_lines)\n\n\n# ----------------------------------------------------------------------------\n# Public entry point\n# ----------------------------------------------------------------------------'

REPLACEMENT_BLOCK_KIT = 'def _render_table(rows: List[str]) -> str:\n    """Render markdown pipe-table rows as aligned monospace text (fallback)."""\n    parsed: List[List[str]] = []\n    for r in rows:\n        cells = _split_row(r)\n        parsed.append(cells)\n    if not parsed:\n        return "\\n".join(rows)\n    ncols = max(len(r) for r in parsed)\n    for r in parsed:\n        r.extend([""] * (ncols - len(r)))\n    widths = [max(len(r[c]) for r in parsed) for c in range(ncols)]\n    out_lines = []\n    for ri, r in enumerate(parsed):\n        line = " | ".join(r[c].ljust(widths[c]) for c in range(ncols))\n        out_lines.append(line.rstrip())\n        if ri == 0:  # header underline\n            out_lines.append("-+-".join("-" * widths[c] for c in range(ncols)))\n    return "\\n".join(out_lines)\n\n\ndef render_tables_as_fenced_monospace(markdown: str) -> str:\n    """Replace markdown pipe-tables with fenced aligned-monospace text.\n\n    Vicegerent patch: with Slack rich_blocks (Block Kit) disabled entirely to\n    avoid its per-block "Show more"/"Show less" collapse chrome, this is the\n    flat-``text``-only alternative for making tables still readable. Runs\n    BEFORE format_message()\'s own markdown->mrkdwn pipeline, so the fenced\n    blocks it emits are protected verbatim by that pipeline\'s existing\n    fenced-code-block step. Never raises: any unexpected input is returned\n    unmodified (falls through to being rendered as raw pipes/dashes, same as\n    if this function didn\'t exist).\n    """\n    if not markdown or "|" not in markdown:\n        return markdown\n    try:\n        lines = markdown.replace("\\r\\n", "\\n").split("\\n")\n        out: List[str] = []\n        i = 0\n        n = len(lines)\n        while i < n:\n            line = lines[i]\n            if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):\n                header_row = line\n                sep_line = lines[i + 1]\n                trows = [header_row]\n                i += 2\n                while i < n and "|" in lines[i] and lines[i].strip():\n                    trows.append(lines[i])\n                    i += 1\n                rendered = _render_table(trows)\n                out.append("```\\n" + rendered + "\\n```")\n                continue\n            out.append(line)\n            i += 1\n        return "\\n".join(out)\n    except Exception:\n        return markdown\n\n\n# ----------------------------------------------------------------------------\n# Public entry point\n# ----------------------------------------------------------------------------'

# --- adapter.py: import the new function ------------------------------------

ANCHOR_ADAPTER_IMPORT = 'try:  # sibling module; support both package and flat plugin-dir import\n    from .block_kit import render_blocks\nexcept ImportError:  # pragma: no cover - plugin loaded outside package context\n    from block_kit import render_blocks  # type: ignore'

REPLACEMENT_ADAPTER_IMPORT = 'try:  # sibling module; support both package and flat plugin-dir import\n    from .block_kit import render_blocks, render_tables_as_fenced_monospace\nexcept ImportError:  # pragma: no cover - plugin loaded outside package context\n    from block_kit import render_blocks, render_tables_as_fenced_monospace  # type: ignore'

# --- adapter.py: call it as the first step of format_message() -------------

ANCHOR_ADAPTER_FORMAT_MESSAGE = '    def format_message(self, content: str) -> str:\n        """Convert standard markdown to Slack mrkdwn format.\n\n        Protected regions (code blocks, inline code) are extracted first so\n        their contents are never modified.  Standard markdown constructs\n        (headers, bold, italic, links) are translated to mrkdwn syntax.\n        """\n        if not content:\n            return content\n\n        placeholders: dict = {}\n        counter = [0]\n\n        def _ph(value: str) -> str:\n            """Stash value behind a placeholder that survives later passes."""\n            key = f"\\x00SL{counter[0]}\\x00"\n            counter[0] += 1\n            placeholders[key] = value\n            return key\n\n        text = content\n\n        # 1) Protect fenced code blocks (``` ... ```)\n        text = re.sub(\n            r"(```(?:[^\\n]*\\n)?[\\s\\S]*?```)",\n            lambda m: _ph(m.group(0)),\n            text,\n        )'

REPLACEMENT_ADAPTER_FORMAT_MESSAGE = '    def format_message(self, content: str) -> str:\n        """Convert standard markdown to Slack mrkdwn format.\n\n        Protected regions (code blocks, inline code) are extracted first so\n        their contents are never modified.  Standard markdown constructs\n        (headers, bold, italic, links) are translated to mrkdwn syntax.\n\n        Vicegerent patch: markdown pipe-tables are rewritten to fenced\n        aligned-monospace text FIRST (see render_tables_as_fenced_monospace),\n        before any other step. With rich_blocks disabled, this is the only\n        remaining table-rendering path -- Slack\'s Block Kit table block\n        carried its own "Show more"/"Show less" collapse chrome on every\n        surrounding section, which was worse than the raw-pipe rendering it\n        replaced. A fenced block survives the existing code-block-protection\n        step below verbatim.\n        """\n        if not content:\n            return content\n\n        content = render_tables_as_fenced_monospace(content)\n\n        placeholders: dict = {}\n        counter = [0]\n\n        def _ph(value: str) -> str:\n            """Stash value behind a placeholder that survives later passes."""\n            key = f"\\x00SL{counter[0]}\\x00"\n            counter[0] += 1\n            placeholders[key] = value\n            return key\n\n        text = content\n\n        # 1) Protect fenced code blocks (``` ... ```)\n        text = re.sub(\n            r"(```(?:[^\\n]*\\n)?[\\s\\S]*?```)",\n            lambda m: _ph(m.group(0)),\n            text,\n        )'

APPLIED_MARKER = "Vicegerent patch: Slack flat-text table rendering"


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _patch_block_kit() -> None:
    spec = importlib.util.find_spec("plugins.platforms.slack.block_kit")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate plugins/platforms/slack/block_kit.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_BLOCK_KIT, path, "_render_table()/public-entry-point boundary")
    src = src.replace(ANCHOR_BLOCK_KIT, REPLACEMENT_BLOCK_KIT, 1)

    src += (
        f"\n\n# {APPLIED_MARKER}: added render_tables_as_fenced_monospace(), "
        "called from SlackAdapter.format_message() to rewrite markdown "
        "pipe-tables into fenced aligned-monospace text.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: added render_tables_as_fenced_monospace() to {path}")


def _patch_adapter() -> None:
    spec = importlib.util.find_spec("plugins.platforms.slack.adapter")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate plugins/platforms/slack/adapter.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_ADAPTER_IMPORT, path, "block_kit import")
    src = src.replace(ANCHOR_ADAPTER_IMPORT, REPLACEMENT_ADAPTER_IMPORT, 1)

    _count_or_raise(src, ANCHOR_ADAPTER_FORMAT_MESSAGE, path, "format_message() entry")
    src = src.replace(ANCHOR_ADAPTER_FORMAT_MESSAGE, REPLACEMENT_ADAPTER_FORMAT_MESSAGE, 1)

    src += (
        f"\n\n# {APPLIED_MARKER}: format_message() now rewrites markdown "
        "pipe-tables to fenced aligned-monospace text before any other "
        "markdown -> mrkdwn conversion step.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: wired render_tables_as_fenced_monospace() into format_message() in {path}")


def main() -> int:
    _patch_block_kit()
    _patch_adapter()
    return 0


if __name__ == "__main__":
    sys.exit(main())
