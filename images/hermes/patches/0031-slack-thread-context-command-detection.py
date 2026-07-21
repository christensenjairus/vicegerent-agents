#!/usr/bin/env python3
"""Vicegerent patch: fix two independent bugs in Slack's
``_handle_slack_message`` message-text assembly that both silently corrupt
in-thread/bang-command handling (``/model``, ``!model``, ``/reasoning``,
``!reasoning``, ``/stop``, etc.).

Fix 1: thread-context injection masking command detection
-----------------------------------------------------------
When a user's message lands inside an EXISTING Slack thread that doesn't
yet have an active gateway session (typically: replying deep in a
long-running thread), ``_handle_slack_message`` fetches a summary of the
thread's prior messages and prepends it directly onto the local ``text``
variable:

    if thread_context:
        text = thread_context + text

That same (now-prefixed) ``text`` is the value handed to
``MessageEvent(text=text, ...)`` a few dozen lines later. Command
detection everywhere downstream -- ``MessageEvent.is_command()``
(``gateway/platforms/base.py``, checks ``self.text.startswith("/")``) and
every ``gateway/run.py`` command dispatcher that calls
``event.get_command()`` -- reads that exact ``event.text`` field. Once the
thread-context block (which always starts with the literal string
``"[Thread context"``) is glued onto the front, ``event.text`` no longer
starts with ``/`` (or the rewritten ``!``->``/`` prefix -- see the ``!``
rewrite logic just below, which happens on ``original_text`` BEFORE this
concatenation and is therefore unaffected itself). The message is
silently reclassified as ordinary chat text and every slash command
silently no-ops for the rest of that thread turn: ``/model``, ``!model``,
``/reasoning``, ``!reasoning``, ``/stop``, ``/queue``, etc. all fail to
dispatch, with zero error message -- the agent just answers the literal
text conversationally instead.

Confirmed live: a user sending ``!model haiku-4-5`` deep in an existing
Slack thread got a normal conversational reply instead of a model switch,
while the exact same command sent as a fresh top-level message (or in a
brand-new thread, before any thread-context fetch triggers) worked
correctly. ``gateway/run.py``'s dispatchers (``_handle_model_command``,
``_handle_reasoning_command``, the ``/stop``/``/queue`` handlers, etc.)
all gate on ``event.get_command()`` (-> ``event.is_command()`` ->
``self.text.startswith("/")``), so this is a systemic gap across every
registered command, not specific to ``/model``.

The gap is entirely avoidable: ``MessageEvent`` already has a dedicated
``channel_context`` field for exactly this "prepend background context
without touching the trigger message's own text" use case --
``gateway/platforms/base.py``'s docstring for it says verbatim: "Kept
separate from ``text`` so the sender-prefix logic in run.py can operate
on the trigger message alone, then prepend this context afterward."
``gateway/run.py`` already consumes it correctly and late (well after
every command-dispatch check has already run):

    if getattr(event, "channel_context", None):
        message_text = f"{event.channel_context}\\n\\n[New message]\\n{message_text}"

The Slack adapter's Discord/Telegram siblings already route their own
history-backfill context through this field. Slack's thread-context path
is the outlier that manually concatenates into ``text`` instead.

Fix: stop mutating ``text`` with the thread-context prefix; keep ``text``
as the raw (mention-stripped) trigger message throughout, stash the
fetched context in a new ``_slack_thread_channel_context`` local instead,
and pass it through ``MessageEvent(..., channel_context=..., ...)`` so
``gateway/run.py`` still gets the full thread background -- just via the
field designed for it, applied after command dispatch instead of before.

Fix 2: blocks-merge dedup duplicating bang-command text
-----------------------------------------------------------
``_handle_slack_message`` rewrites a leading ``!`` to ``/`` on
``original_text`` (so bang-commands dispatch like real slash commands),
then sets ``text = original_text`` and merges in any text found in
Slack's ``blocks`` array (extracted via ``_extract_text_from_slack_blocks``)
-- this exists to surface quoted/forwarded message content that Slack's
plain ``text`` field omits. The merge dedupes against accidental
double-inclusion:

    stripped_blocks = blocks_text.strip()
    if stripped_blocks and stripped_blocks not in text.strip():
        text = (text.strip() + "\\n" + stripped_blocks).strip()

The bug: Slack's ``blocks`` array always mirrors the message's RAW,
un-rewritten text -- it is a verbatim representation of what the user
typed, never touched by the ``!``->``/`` rewrite above. So for any
"!command ..." message, ``blocks_text`` is the raw ``"!reasoning high"``
while ``text`` is the rewritten ``"/reasoning high"``. The substring check
``stripped_blocks not in text.strip()`` is always true (the raw form never
appears inside the rewritten form), so the dedup silently fails and the
raw command text gets appended a second time:

    text == "/reasoning high\\n!reasoning high"

Downstream, ``MessageEvent.get_command_args()`` splits on the first
whitespace and returns everything after it as the args string -- which is
now ``"high\\n!reasoning high"`` instead of just ``"high"``. Every command
handler that then tokenizes/joins that args string (confirmed live for
``_parse_reasoning_command_args`` in ``gateway/run.py``, which
``shlex.split()``s and rejoins the tokens) produces a garbage value:

    /reasoning high !reasoning high
    -> ⚠️ Unknown argument: `high !reasoning high`

Any other single-line ``!command <args>`` (``!model <name>``, etc.) is
exposed to the same corruption whenever Slack includes a ``blocks`` array
for the message (Slack's modern composer emits one for essentially every
message, not just genuine quotes/forwards -- confirmed live for a plain
``!reasoning high`` message with no quoting/forwarding involved at all).

Fix: capture the message's raw, pre-rewrite text in a dedicated local
(``_raw_slack_text``) at the point ``original_text`` is first read from the
event, before the ``!``->``/`` rewrite can mutate it. Use that raw value
(instead of the possibly-rewritten ``text``) as the dedup comparison
target -- ``blocks_text`` is raw, so comparing raw-to-raw correctly detects
"this block content is just an echo of the plain text field the user
typed" for every command form, rewritten or not. Non-command messages are
unaffected: ``original_text`` is never mutated for them, so
``_raw_slack_text == original_text == text`` and the dedup behaves
identically to before.

Both fixes are purely routing fixes: what content is fetched/merged, and
when, are unchanged in both cases -- only where each value ends up is
different. Fail-loud by design: each fix's own anchors are counted
independently, and if either is absent or appears an unexpected number of
times (upstream refactored this function), the patch raises and the image
build fails, signalling a re-verify. Each fix also short-circuits to a
no-op independently (via its own marker check), so re-running this script
after only one fix has landed on a given source tree still correctly
applies just the missing one.

Remove once upstream Hermes routes Slack's thread-context injection
through ``MessageEvent.channel_context`` itself instead of concatenating
into ``text``, AND its blocks-merge dedup compares against the message's
raw pre-rewrite text.
"""
import importlib.util
import sys

# --- Fix 1: thread-context text-mutation masking is_command() -------------

ANCHOR_MUTATE_TEXT = (
    "        # When entering a thread for the first time (no existing session),\n"
    "        # fetch thread context so the agent understands the conversation.\n"
    "        if is_thread_reply and not self._has_active_session_for_thread(\n"
    "            channel_id=channel_id,\n"
    "            thread_ts=event_thread_ts,\n"
    "            user_id=user_id,\n"
    "            team_id=team_id,\n"
    "        ):\n"
    "            thread_context = await self._fetch_thread_context(\n"
    "                channel_id=channel_id,\n"
    "                thread_ts=event_thread_ts,\n"
    "                current_ts=ts,\n"
    "                team_id=team_id,\n"
    "            )\n"
    "            if thread_context:\n"
    "                text = thread_context + text\n"
)

REPLACEMENT_MUTATE_TEXT = (
    "        # When entering a thread for the first time (no existing session),\n"
    "        # fetch thread context so the agent understands the conversation.\n"
    "        #\n"
    "        # Vicegerent patch 0031: initialize this unconditionally (not\n"
    "        # just inside the branch below) so the MessageEvent(...)\n"
    "        # constructor further down can always reference it by name.\n"
    "        _slack_thread_channel_context = None\n"
    "        if is_thread_reply and not self._has_active_session_for_thread(\n"
    "            channel_id=channel_id,\n"
    "            thread_ts=event_thread_ts,\n"
    "            user_id=user_id,\n"
    "            team_id=team_id,\n"
    "        ):\n"
    "            thread_context = await self._fetch_thread_context(\n"
    "                channel_id=channel_id,\n"
    "                thread_ts=event_thread_ts,\n"
    "                current_ts=ts,\n"
    "                team_id=team_id,\n"
    "            )\n"
    "            # Vicegerent patch 0031: do NOT prepend into `text` -- that\n"
    "            # masks in-thread /model, !model, /reasoning, !reasoning,\n"
    "            # /stop, etc. by making `event.text` no longer start with\n"
    "            # \"/\" (or the rewritten \"!\"), so every downstream\n"
    "            # `event.get_command()` check silently returns None and the\n"
    "            # command is treated as plain chat text instead of\n"
    "            # dispatching. Stash it for the MessageEvent's dedicated\n"
    "            # `channel_context` field instead (see anchor 2 below),\n"
    "            # which gateway/run.py already applies AFTER command\n"
    "            # dispatch has already run.\n"
    "            if thread_context:\n"
    "                _slack_thread_channel_context = thread_context\n"
)

ANCHOR_MESSAGE_EVENT_CHANNEL_PROMPT = (
    "            channel_prompt=_channel_prompt,\n"
)

REPLACEMENT_MESSAGE_EVENT_CHANNEL_PROMPT = (
    "            channel_prompt=_channel_prompt,\n"
    "            # Vicegerent patch 0031: routed via the dedicated field\n"
    "            # instead of mutating `text` above -- see anchor 1's\n"
    "            # replacement for why.\n"
    "            channel_context=_slack_thread_channel_context,\n"
)

FIX1_MARKER = "Vicegerent patch 0031"

# --- Fix 2: blocks-merge dedup comparing against rewritten text -----------

ANCHOR_CAPTURE_RAW_TEXT = (
    "        original_text = event.get(\"text\", \"\")\n"
    "\n"
    "        # Slack blocks native slash commands inside threads (\"/queue is not\n"
)

REPLACEMENT_CAPTURE_RAW_TEXT = (
    "        original_text = event.get(\"text\", \"\")\n"
    "        # Vicegerent patch 0031 (fix 2): keep the pre-rewrite raw text\n"
    "        # around. Slack's `blocks` array always mirrors the message's\n"
    "        # RAW text (never touched by the \"!\"->\"/ \" rewrite below), but\n"
    "        # the blocks-merge dedup further down compares against `text`\n"
    "        # (derived from the possibly-rewritten `original_text`). For\n"
    "        # any \"!command ...\" message that comparison always misses, so\n"
    "        # the raw command text gets appended a second time -- see the\n"
    "        # dedup-check anchor below.\n"
    "        _raw_slack_text = original_text\n"
    "\n"
    "        # Slack blocks native slash commands inside threads (\"/queue is not\n"
)

ANCHOR_DEDUP_CHECK = (
    "                stripped_blocks = blocks_text.strip()\n"
    "                if stripped_blocks and stripped_blocks not in text.strip():\n"
)

REPLACEMENT_DEDUP_CHECK = (
    "                stripped_blocks = blocks_text.strip()\n"
    "                # Vicegerent patch 0031 (fix 2): compare against the\n"
    "                # raw, pre-rewrite text (see above) instead of `text`,\n"
    "                # which may have had \"!\"->\"/ \" applied. `blocks_text`\n"
    "                # is always raw, so raw-to-raw is the correct dedup\n"
    "                # comparison for both command and non-command messages.\n"
    "                if stripped_blocks and stripped_blocks not in _raw_slack_text.strip():\n"
)

FIX2_MARKER = "Vicegerent patch 0031 (fix 2)"


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _apply_fix1(src: str, path: str) -> str:
    if FIX1_MARKER in src:
        print(f"patch: fix 1 (thread-context command masking) already applied to {path} -- no-op")
        return src

    _count_or_raise(src, ANCHOR_MUTATE_TEXT, path, "thread_context text-mutation")
    src = src.replace(ANCHOR_MUTATE_TEXT, REPLACEMENT_MUTATE_TEXT, 1)

    _count_or_raise(
        src, ANCHOR_MESSAGE_EVENT_CHANNEL_PROMPT, path, "MessageEvent channel_prompt kwarg"
    )
    src = src.replace(
        ANCHOR_MESSAGE_EVENT_CHANNEL_PROMPT, REPLACEMENT_MESSAGE_EVENT_CHANNEL_PROMPT, 1
    )

    src += (
        f"\n# {FIX1_MARKER}: Slack thread-context now routed through "
        "MessageEvent.channel_context instead of mutating `text`, so "
        "in-thread /model, !model, /reasoning, !reasoning, /stop, etc. "
        "are correctly recognized as commands again.\n"
    )
    print(
        f"patch: Slack thread-context injection no longer masks in-thread "
        f"commands in {path}"
    )
    return src


def _apply_fix2(src: str, path: str) -> str:
    if FIX2_MARKER in src:
        print(f"patch: fix 2 (blocks-merge dedup) already applied to {path} -- no-op")
        return src

    _count_or_raise(src, ANCHOR_CAPTURE_RAW_TEXT, path, "raw-text capture")
    src = src.replace(ANCHOR_CAPTURE_RAW_TEXT, REPLACEMENT_CAPTURE_RAW_TEXT, 1)

    _count_or_raise(src, ANCHOR_DEDUP_CHECK, path, "blocks-merge dedup check")
    src = src.replace(ANCHOR_DEDUP_CHECK, REPLACEMENT_DEDUP_CHECK, 1)

    src += (
        f"\n# {FIX2_MARKER}: Slack blocks-merge dedup now compares against "
        "the raw pre-rewrite text, so bang-commands (!model, !reasoning, "
        "etc.) no longer get their own raw text duplicated onto `text`, "
        "which was corrupting command argument parsing.\n"
    )
    print(
        f"patch: Slack blocks-merge dedup no longer duplicates bang-command "
        f"text in {path}"
    )
    return src


def main() -> int:
    spec = importlib.util.find_spec("plugins.platforms.slack.adapter")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate plugins/platforms/slack/adapter.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if FIX1_MARKER in src and FIX2_MARKER in src:
        print(f"patch: already fully applied to {path} -- no-op")
        return 0

    src = _apply_fix1(src, path)
    src = _apply_fix2(src, path)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    return 0


if __name__ == "__main__":
    sys.exit(main())
