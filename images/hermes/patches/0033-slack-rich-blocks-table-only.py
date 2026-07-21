#!/usr/bin/env python3
"""Vicegerent patch: stop Slack rich-blocks rendering from exploding ordinary
prose (headers, lists, quotes, horizontal rules, fenced code, paragraphs)
into one Block Kit block per structural element, which inflates a message's
rendered height enough to trigger Slack's "Show More" collapse -- while
keeping markdown pipe-tables rendered as native Block Kit ``table`` blocks
(the entire reason ``slack.extra.rich_blocks`` was enabled).

Context
-------
``render_blocks()`` (``plugins/platforms/slack/block_kit.py``) previously
walked the agent's markdown and emitted a SEPARATE block for every
structural element it recognized: an ATX header became its own ``header``
block, a horizontal rule its own ``divider``, each contiguous list run its
own ``rich_text`` block, each blockquote group its own ``rich_text_quote``
block, and each paragraph its own ``section`` block. Markdown pipe-tables
already got their own native ``table`` block (unchanged by this patch).

Slack renders every block with its own padding/margins. A response that
mixes a couple of headers, a short list, and two or three paragraphs --
content that fit comfortably as flat mrkdwn ``text`` before rich_blocks was
enabled -- can explode into 8-10+ separate blocks, and the CUMULATIVE
rendered height crosses Slack's client-side collapse threshold even though
the underlying text is not particularly long. The user has to click "Show
More" to see the rest of an otherwise-short response.

Confirmed live: enabling ``slack.extra.rich_blocks`` made ordinary
multi-paragraph responses collapse behind "Show More" in the Slack client
that did not collapse when sent as flat mrkdwn ``text`` of comparable
length. Disabling rich_blocks entirely removes the collapse but also loses
native table rendering, which was the reason it was enabled in the first
place.

Fix
---
Restructure ``render_blocks()`` so ONLY markdown pipe-tables are recognized
as their own structural primitive. Every other line (headers, lists,
quotes, horizontal rules, fenced code, plain paragraphs) is accumulated
into a single running text buffer and flushed as flat mrkdwn ``section``
block(s) -- identical in spirit to how ``format_message()`` rendered the
same content before rich_blocks existed, just wrapped in a ``section``
block instead of the top-level ``text`` payload. The buffer is flushed (1)
whenever a table boundary is hit, so table blocks stay correctly
interleaved with the prose around them, and (2) at the 3000-character
``section``/text object limit, exactly as the original per-paragraph path
already did.

Net effect: a response with N tables and arbitrary surrounding prose now
produces roughly 2N+1 blocks (prose, table, prose, table, ..., prose)
instead of one block per header/list/quote/paragraph -- tables still
render as real grid cells with per-column alignment (unchanged), but
everything else renders at close to its pre-rich-blocks compactness.

Verified structurally (anchor matches exactly once against the live
source, patched output compiles) and behaviorally: pure prose still
collapses to a single ``section`` block; a table with surrounding prose
produces ``[section, table, section]``; multiple tables interleave
correctly; oversized single-buffer content still splits at the 3000-char
cap; tables exceeding Slack's own structural limits (100 rows / 20 cols /
10k aggregate chars) still fall back to a monospace ``rich_text_preformatted``
block as before; the ``MAX_BLOCKS`` (50) safety cap still triggers a full
``None`` fallback to plain text for pathological inputs; empty/whitespace
input still returns ``None``.

The now-unused ``_header_block``/``_divider_block``/``_quote_block``/
``_list_block`` builder functions (and their supporting regexes/helpers)
are left in place rather than deleted -- they are small, harmless dead
code, and keeping them minimizes this patch's diff and makes it trivially
revertable if a future iteration wants to selectively re-enable one of
them (e.g. headers only) instead of the current all-or-nothing table-only
scope.

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream refactored this function), the patch raises and
the image build fails, signalling a re-verify.

Remove once upstream Hermes's Slack rich-blocks renderer itself only
promotes tables to structural blocks and folds everything else into flat
mrkdwn sections, or once Slack's client-side "Show More" threshold no
longer triggers on the previous per-element block explosion.
"""
import importlib.util
import sys

ANCHOR_RENDER_BLOCKS = 'def render_blocks(\n    markdown: str,\n    mrkdwn_fn=None,\n) -> Optional[List[Block]]:\n    """Convert agent markdown to a Slack Block Kit ``blocks`` list.\n\n    Args:\n        markdown: The agent\'s response text (standard markdown).\n        mrkdwn_fn: Optional callable converting a markdown paragraph to Slack\n            mrkdwn for ``section`` blocks (the adapter passes\n            ``format_message``).  When ``None``, the raw paragraph text is used.\n\n    Returns:\n        A list of Block Kit block dicts, or ``None`` when the content is empty,\n        exceeds Slack\'s structural limits, or hits an unexpected shape — the\n        caller then falls back to the flat ``text`` payload.  Never raises.\n    """\n    if not markdown or not markdown.strip():\n        return None\n\n    fmt = mrkdwn_fn or (lambda s: s)\n\n    try:\n        blocks: List[Block] = []\n        lines = markdown.replace("\\r\\n", "\\n").split("\\n")\n        i = 0\n        n = len(lines)\n        para: List[str] = []\n\n        def flush_para() -> None:\n            if not para:\n                return\n            text = "\\n".join(para).strip()\n            para.clear()\n            if not text:\n                return\n            rendered = fmt(text)\n            # Split oversized sections on the 3000-char limit.\n            for chunk in _split_text(rendered, MAX_SECTION_TEXT):\n                blocks.append(_section_block(chunk))\n\n        while i < n:\n            line = lines[i]\n\n            # Blank line: paragraph boundary\n            if not line.strip():\n                flush_para()\n                i += 1\n                continue\n\n            # Fenced code block\n            fence = _FENCE_RE.match(line)\n            if fence:\n                flush_para()\n                marker = fence.group(1)\n                body: List[str] = []\n                i += 1\n                while i < n and not lines[i].lstrip().startswith(marker):\n                    body.append(lines[i])\n                    i += 1\n                i += 1  # consume closing fence\n                blocks.append(_preformatted_block("\\n".join(body)))\n                continue\n\n            # Horizontal rule → divider\n            if _HR_RE.match(line):\n                flush_para()\n                blocks.append(_divider_block())\n                i += 1\n                continue\n\n            # ATX header\n            hm = _HEADER_RE.match(line)\n            if hm:\n                flush_para()\n                blocks.append(_header_block(hm.group(2)))\n                i += 1\n                continue\n\n            # Pipe table: current line has a pipe AND next line is a separator\n            if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):\n                flush_para()\n                header_row = line\n                sep_line = lines[i + 1]\n                trows = [header_row]\n                i += 2  # skip header + separator\n                while i < n and "|" in lines[i] and lines[i].strip():\n                    trows.append(lines[i])\n                    i += 1\n                # Prefer a native Block Kit table; fall back to aligned\n                # monospace when it exceeds Slack\'s table limits or won\'t parse.\n                table = _table_block(trows, sep_line)\n                if table is not None:\n                    blocks.append(table)\n                else:\n                    blocks.append(_preformatted_block(_render_table(trows)))\n                continue\n\n            # Blockquote group\n            if _QUOTE_RE.match(line):\n                flush_para()\n                qlines: List[str] = []\n                while i < n:\n                    qm = _QUOTE_RE.match(lines[i])\n                    if not qm:\n                        break\n                    qlines.append(qm.group(1))\n                    i += 1\n                blocks.append(_quote_block(qlines))\n                continue\n\n            # List group (bullets + ordered, with nesting)\n            if _is_list_line(line):\n                flush_para()\n                items: List[Tuple[int, bool, str]] = []\n                while i < n:\n                    bm = _BULLET_RE.match(lines[i])\n                    om = _ORDERED_RE.match(lines[i])\n                    if bm:\n                        items.append((_indent_level(bm.group(1)), False, bm.group(2)))\n                        i += 1\n                    elif om:\n                        items.append((_indent_level(om.group(1)), True, om.group(3)))\n                        i += 1\n                    elif lines[i].strip() and lines[i].startswith((" ", "\\t")) and items:\n                        # continuation line of the previous item\n                        indent, ordered, txt = items[-1]\n                        items[-1] = (indent, ordered, txt + " " + lines[i].strip())\n                        i += 1\n                    elif not lines[i].strip() and items:\n                        # Blank line inside a list run. LLM-authored ordered\n                        # lists commonly separate items with a blank line; if\n                        # the next non-blank line is another list item, treat\n                        # the blank(s) as a soft separator and keep the run\n                        # going so the items stay in one rich_text_list (Slack\n                        # numbers each list independently, so splitting would\n                        # restart every item at "1."). Otherwise the blank\n                        # ends the list.\n                        j = i + 1\n                        while j < n and not lines[j].strip():\n                            j += 1\n                        if j < n and _is_list_line(lines[j]):\n                            i = j\n                        else:\n                            break\n                    else:\n                        break\n                blocks.append(_list_block(items))\n                continue\n\n            # Default: accumulate into a paragraph\n            para.append(line)\n            i += 1\n\n        flush_para()\n\n        if not blocks:\n            return None\n        if len(blocks) > MAX_BLOCKS:\n            # Too structurally complex to express safely — let the caller fall\n            # back to plain text rather than truncating and losing content.\n            return None\n        return blocks\n    except Exception:\n        # Never let a rendering bug drop a message.\n        return None\n'

REPLACEMENT_RENDER_BLOCKS = 'def render_blocks(\n    markdown: str,\n    mrkdwn_fn=None,\n) -> Optional[List[Block]]:\n    """Convert agent markdown to a Slack Block Kit ``blocks`` list.\n\n    Args:\n        markdown: The agent\'s response text (standard markdown).\n        mrkdwn_fn: Optional callable converting a markdown paragraph to Slack\n            mrkdwn for ``section`` blocks (the adapter passes\n            ``format_message``).  When ``None``, the raw paragraph text is used.\n\n    Returns:\n        A list of Block Kit block dicts, or ``None`` when the content is empty,\n        exceeds Slack\'s structural limits, or hits an unexpected shape -- the\n        caller then falls back to the flat ``text`` payload.  Never raises.\n\n    Vicegerent patch: ONLY markdown pipe-tables are rendered as their own\n    structural Block Kit primitive (native ``table`` block). Every other\n    construct (headers, lists, quotes, horizontal rules, fenced code,\n    paragraphs) is left as flat mrkdwn text and folded into the SAME\n    ``section`` block as its surrounding prose, split only across table\n    boundaries and the 3000-char section-text cap. Slack renders every extra\n    block with its own padding, and a message exploded into one\n    header/divider/rich_text_list/rich_text_quote block per line/paragraph\n    grows tall enough to trigger Slack\'s "Show More" collapse even when the\n    underlying content is short -- exactly the tradeoff previous behavior\n    (splitting everything into separate blocks) made visible. Confirmed\n    live: enabling rich_blocks made ordinary multi-paragraph responses\n    collapse behind "Show More" in the Slack client, while flat mrkdwn\n    ``text`` of the same length did not. Folding non-table content back into\n    plain mrkdwn section text (identical to how format_message() rendered it\n    before rich_blocks existed) restores that compactness while keeping\n    tables\' real grid/alignment rendering, which was the entire point of\n    enabling rich_blocks in the first place.\n    """\n    if not markdown or not markdown.strip():\n        return None\n\n    fmt = mrkdwn_fn or (lambda s: s)\n\n    try:\n        blocks: List[Block] = []\n        lines = markdown.replace("\\r\\n", "\\n").split("\\n")\n        i = 0\n        n = len(lines)\n        buf: List[str] = []\n\n        def flush_buf() -> None:\n            if not buf:\n                return\n            text = "\\n".join(buf).strip("\\n")\n            buf.clear()\n            if not text.strip():\n                return\n            rendered = fmt(text)\n            # Split oversized sections on the 3000-char limit.\n            for chunk in _split_text(rendered, MAX_SECTION_TEXT):\n                blocks.append(_section_block(chunk))\n\n        while i < n:\n            line = lines[i]\n\n            # Pipe table: current line has a pipe AND next line is a separator.\n            # This is the ONLY construct that gets its own structural block --\n            # everything else accumulates into `buf` as flat mrkdwn text.\n            if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):\n                flush_buf()\n                header_row = line\n                sep_line = lines[i + 1]\n                trows = [header_row]\n                i += 2  # skip header + separator\n                while i < n and "|" in lines[i] and lines[i].strip():\n                    trows.append(lines[i])\n                    i += 1\n                # Prefer a native Block Kit table; fall back to aligned\n                # monospace when it exceeds Slack\'s table limits or won\'t parse.\n                table = _table_block(trows, sep_line)\n                if table is not None:\n                    blocks.append(table)\n                else:\n                    blocks.append(_preformatted_block(_render_table(trows)))\n                continue\n\n            buf.append(line)\n            i += 1\n\n        flush_buf()\n\n        if not blocks:\n            return None\n        if len(blocks) > MAX_BLOCKS:\n            # Too structurally complex to express safely -- let the caller fall\n            # back to plain text rather than truncating and losing content.\n            return None\n        return blocks\n    except Exception:\n        # Never let a rendering bug drop a message.\n        return None'

APPLIED_MARKER = "Vicegerent patch: Slack rich-blocks table-only rendering"


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def main() -> int:
    spec = importlib.util.find_spec("plugins.platforms.slack.block_kit")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate plugins/platforms/slack/block_kit.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return 0

    _count_or_raise(src, ANCHOR_RENDER_BLOCKS, path, "render_blocks() function body")
    src = src.replace(ANCHOR_RENDER_BLOCKS, REPLACEMENT_RENDER_BLOCKS, 1)

    src += (
        f"\n\n# {APPLIED_MARKER}: render_blocks() now only promotes markdown "
        "pipe-tables to native Block Kit blocks; every other construct "
        "(headers, lists, quotes, horizontal rules, fenced code, paragraphs) "
        "is folded into flat mrkdwn section blocks, restoring pre-rich-blocks "
        "message compactness while keeping native table rendering.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: Slack rich-blocks rendering now only promotes tables to "
        f"structural blocks in {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
