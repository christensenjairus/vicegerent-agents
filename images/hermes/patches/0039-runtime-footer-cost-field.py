#!/usr/bin/env python3
"""Vicegerent patch: replace footer cwd with turn/session cost and duration.

Context
-------
The footer's ``cwd`` is misleading rather than merely approximate.
``gateway/run.py`` copies the configured ``terminal.cwd`` into
``os.environ["TERMINAL_CWD"]`` exactly once when the gateway process starts.
It is not updated by ``/new``, per session, per turn, or when a terminal tool
changes directories, so it has no relationship to where a particular session
or tool call navigated.

Hermes does track ``agent.session_estimated_cost_usd``, but that value is a
running session total. The gateway caches one ``AIAgent`` per session across
turns, and Hermes never resets this counter at a turn boundary, so it is the
right source for session spend but cannot directly show the current turn.

Fix
---
Remove ``cwd`` completely from ``format_runtime_footer()`` and
``build_footer_line()``, including the now-unused ``_home_relative_cwd()`` and
``os`` import. This is a clean removal after patches 0028 and 0036: their
post-patch signatures and DM-only footer call are anchored explicitly here,
while their effort field and Slack gating/italics behavior remain intact.

Snapshot ``agent.session_estimated_cost_usd`` immediately before
``run_conversation()``, read it again immediately afterward, and calculate the
turn delta. Snapshot wall-clock time around the same call to calculate the last
turn's duration. These values are added to the two gateway return dictionaries,
then passed to the footer. A requested ``cost`` field renders both spend
values rounded to two decimal places; ``duration`` renders seconds or compact
minutes and seconds. Missing values are silently skipped like the existing
fields.

Fail-loud by design: every anchor must occur exactly once against the source
after patches 0028 and 0036, or the image build stops for re-verification.

Remove once upstream Hermes natively tracks and renders per-turn/session cost
and duration in its runtime footer.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0039"

# --- gateway/runtime_footer.py anchors -------------------------------------

ANCHOR_MODULE_DOC = (
    '"""Gateway runtime-metadata footer.\n'
    "\n"
    "Renders a compact footer showing runtime state (model, context %, cwd) and\n"
    "appends it to the FINAL message of an agent turn when enabled.  Off by default\n"
    "to keep replies minimal.\n"
    "\n"
    "Config (``~/.hermes/config.yaml``)::\n"
    "\n"
    "    display:\n"
    "      runtime_footer:\n"
    "        enabled: true                       # off by default\n"
    "        fields: [model, context_pct, cwd]   # order shown; drop any to hide\n"
)

REPLACEMENT_MODULE_DOC = (
    '"""Gateway runtime-metadata footer.\n'
    "\n"
    "Renders a compact footer showing runtime state (model, context %, cost, duration) and\n"
    "appends it to the FINAL message of an agent turn when enabled.  Off by default\n"
    "to keep replies minimal.\n"
    "\n"
    "Config (``~/.hermes/config.yaml``)::\n"
    "\n"
    "    display:\n"
    "      runtime_footer:\n"
    "        enabled: true                       # off by default\n"
    "        fields: [model, context_pct, cost, duration]  # order shown; drop any to hide\n"
)

ANCHOR_IMPORTS_AND_DEFAULTS = (
    "import os\n"
    "from typing import Any, Iterable, Optional\n"
    "\n"
    "_DEFAULT_FIELDS: tuple[str, ...] = (\"model\", \"context_pct\", \"cwd\")\n"
)

REPLACEMENT_IMPORTS_AND_DEFAULTS = (
    "from typing import Any, Iterable, Optional\n"
    "\n"
    "_DEFAULT_FIELDS: tuple[str, ...] = (\"model\", \"context_pct\", \"cost\", \"duration\")\n"
)

ANCHOR_CWD_HELPER = (
    "def _home_relative_cwd(cwd: str) -> str:\n"
    "    \"\"\"Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset.\"\"\"\n"
    "    if not cwd:\n"
    "        return \"\"\n"
    "    try:\n"
    "        home = os.path.expanduser(\"~\")\n"
    "        p = os.path.abspath(cwd)\n"
    "        if home and (p == home or p.startswith(home + os.sep)):\n"
    "            return \"~\" + p[len(home):]\n"
    "        return p\n"
    "    except Exception:\n"
    "        return cwd\n"
    "\n"
    "\n"
)

ANCHOR_FORMAT_SIGNATURE = (
    "def format_runtime_footer(\n"
    "    *,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    cwd: Optional[str] = None,\n"
    "    effort: Optional[str] = None,\n"
    "    fields: Iterable[str] = _DEFAULT_FIELDS,\n"
    ") -> str:\n"
)

REPLACEMENT_FORMAT_SIGNATURE = (
    "def format_runtime_footer(\n"
    "    *,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    effort: Optional[str] = None,\n"
    "    turn_cost_usd: Optional[float] = None,\n"
    "    session_cost_usd: Optional[float] = None,\n"
    "    turn_duration_s: Optional[float] = None,\n"
    "    fields: Iterable[str] = _DEFAULT_FIELDS,\n"
    ") -> str:\n"
)

ANCHOR_FORMAT_FIELDS = (
    "        elif field == \"cwd\":\n"
    "            rel = _home_relative_cwd(cwd or os.environ.get(\"TERMINAL_CWD\", \"\"))\n"
    "            if rel:\n"
    "                parts.append(rel)\n"
    "        elif field == \"effort\":\n"
    "            # Vicegerent patch 0028: not an upstream field -- see run.py's\n"
    "            # agent.reasoning_config for where this value comes from.\n"
    "            if effort:\n"
    "                parts.append(str(effort))\n"
    "        # Unknown field names are silently ignored.\n"
)

REPLACEMENT_FORMAT_FIELDS = (
    "        elif field == \"effort\":\n"
    "            # Vicegerent patch 0028: not an upstream field -- see run.py's\n"
    "            # agent.reasoning_config for where this value comes from.\n"
    "            if effort:\n"
    "                parts.append(str(effort))\n"
    "        elif field == \"cost\":\n"
    "            if turn_cost_usd is not None and session_cost_usd is not None:\n"
    "                parts.append(\n"
    "                    f\"${turn_cost_usd:.2f} turn · ${session_cost_usd:.2f} session\"\n"
    "                )\n"
    "        elif field == \"duration\":\n"
    "            if turn_duration_s is not None:\n"
    "                if turn_duration_s < 60:\n"
    "                    parts.append(f\"{turn_duration_s:.1f}s\")\n"
    "                else:\n"
    "                    minutes, seconds = divmod(round(turn_duration_s), 60)\n"
    "                    parts.append(f\"{minutes}m {seconds}s\")\n"
    "        # Unknown field names are silently ignored.\n"
)

ANCHOR_BUILD_SIGNATURE = (
    "def build_footer_line(\n"
    "    *,\n"
    "    user_config: dict[str, Any] | None,\n"
    "    platform_key: str | None,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    cwd: Optional[str] = None,\n"
    "    effort: Optional[str] = None,\n"
    ") -> str:\n"
)

REPLACEMENT_BUILD_SIGNATURE = (
    "def build_footer_line(\n"
    "    *,\n"
    "    user_config: dict[str, Any] | None,\n"
    "    platform_key: str | None,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    effort: Optional[str] = None,\n"
    "    turn_cost_usd: Optional[float] = None,\n"
    "    session_cost_usd: Optional[float] = None,\n"
    "    turn_duration_s: Optional[float] = None,\n"
    ") -> str:\n"
)

ANCHOR_BUILD_CALL = (
    "    return format_runtime_footer(\n"
    "        model=model,\n"
    "        context_tokens=context_tokens,\n"
    "        context_length=context_length,\n"
    "        cwd=cwd,\n"
    "        effort=effort,\n"
    "        fields=cfg.get(\"fields\") or _DEFAULT_FIELDS,\n"
    "    )\n"
)

REPLACEMENT_BUILD_CALL = (
    "    return format_runtime_footer(\n"
    "        model=model,\n"
    "        context_tokens=context_tokens,\n"
    "        context_length=context_length,\n"
    "        effort=effort,\n"
    "        turn_cost_usd=turn_cost_usd,\n"
    "        session_cost_usd=session_cost_usd,\n"
    "        turn_duration_s=turn_duration_s,\n"
    "        fields=cfg.get(\"fields\") or _DEFAULT_FIELDS,\n"
    "    )\n"
)

# --- gateway/run.py anchors -------------------------------------------------

ANCHOR_RUN_CONVERSATION = (
    "                if _persist_user_timestamp_override is not None:\n"
    "                    _conversation_kwargs[\"persist_user_timestamp\"] = _persist_user_timestamp_override\n"
    "                result = agent.run_conversation(_api_run_message, **_conversation_kwargs)\n"
)

REPLACEMENT_RUN_CONVERSATION = (
    "                if _persist_user_timestamp_override is not None:\n"
    "                    _conversation_kwargs[\"persist_user_timestamp\"] = _persist_user_timestamp_override\n"
    "                _session_cost_before = getattr(\n"
    "                    agent, \"session_estimated_cost_usd\", None\n"
    "                )\n"
    "                _turn_started_at = time.time()\n"
    "                result = agent.run_conversation(_api_run_message, **_conversation_kwargs)\n"
    "                _turn_duration_s = max(0.0, time.time() - _turn_started_at)\n"
    "                _session_cost_after = getattr(\n"
    "                    agent, \"session_estimated_cost_usd\", None\n"
    "                )\n"
    "                _session_cost_available = (\n"
    "                    getattr(agent, \"session_cost_status\", \"unknown\") != \"unknown\"\n"
    "                    and _session_cost_before is not None\n"
    "                    and _session_cost_after is not None\n"
    "                )\n"
    "                _turn_cost_usd = (\n"
    "                    max(0.0, float(_session_cost_after) - float(_session_cost_before))\n"
    "                    if _session_cost_available\n"
    "                    else None\n"
    "                )\n"
    "                if not _session_cost_available:\n"
    "                    _session_cost_after = None\n"
)

ANCHOR_SLACK_FOOTER_COMMENT = (
    "            # a footer showing model/context/cwd is useful in a private\n"
    "            # 1:1 with the bot but leaks internal runtime details (cwd,\n"
    "            # active model) into a shared channel with colleagues who never\n"
)

REPLACEMENT_SLACK_FOOTER_COMMENT = (
    "            # a footer showing model/context/cost is useful in a private\n"
    "            # 1:1 with the bot but leaks internal runtime details (cost,\n"
    "            # active model) into a shared channel with colleagues who never\n"
)

ANCHOR_SLACK_TRAILING_MARKER = (
    "# Vicegerent patch: Slack runtime footer DM-only: the runtime footer "
    "(model/context/effort/cwd) is now suppressed on Slack outside DMs "
)

REPLACEMENT_SLACK_TRAILING_MARKER = (
    "# Vicegerent patch: Slack runtime footer DM-only: the runtime footer "
    "(model/context/effort/cost) is now suppressed on Slack outside DMs "
)

ANCHOR_EARLY_RETURN = (
    "                    \"context_length\": _context_length,\n"
    "                    # Vicegerent patch 0028: feeds runtime_footer.py's\n"
)

REPLACEMENT_EARLY_RETURN = (
    "                    \"context_length\": _context_length,\n"
    "                    \"turn_cost_usd\": _turn_cost_usd,\n"
    "                    \"session_cost_usd\": _session_cost_after,\n"
    "                    \"turn_duration_s\": _turn_duration_s,\n"
    "                    # Vicegerent patch 0028: feeds runtime_footer.py's\n"
)

ANCHOR_FINAL_RETURN = (
    "                \"context_length\": _context_length,\n"
    "                # Vicegerent patch 0028: feeds runtime_footer.py's \"effort\"\n"
)

REPLACEMENT_FINAL_RETURN = (
    "                \"context_length\": _context_length,\n"
    "                \"turn_cost_usd\": _turn_cost_usd,\n"
    "                \"session_cost_usd\": _session_cost_after,\n"
    "                \"turn_duration_s\": _turn_duration_s,\n"
    "                # Vicegerent patch 0028: feeds runtime_footer.py's \"effort\"\n"
)

ANCHOR_FOOTER_CALL = (
    "                        context_length=agent_result.get(\"context_length\") or None,\n"
    "                        cwd=os.environ.get(\"TERMINAL_CWD\", \"\"),\n"
    "                        # Vicegerent patch 0028: see runtime_footer.py for the\n"
    "                        # other half of this field.\n"
    "                        effort=agent_result.get(\"reasoning_effort\") or None,\n"
)

REPLACEMENT_FOOTER_CALL = (
    "                        context_length=agent_result.get(\"context_length\") or None,\n"
    "                        # Vicegerent patch 0028: see runtime_footer.py for the\n"
    "                        # other half of this field.\n"
    "                        effort=agent_result.get(\"reasoning_effort\") or None,\n"
    "                        turn_cost_usd=agent_result.get(\"turn_cost_usd\"),\n"
    "                        session_cost_usd=agent_result.get(\"session_cost_usd\"),\n"
    "                        turn_duration_s=agent_result.get(\"turn_duration_s\"),\n"
)


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _patch_runtime_footer() -> None:
    spec = importlib.util.find_spec("gateway.runtime_footer")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/runtime_footer.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    anchors = (
        (ANCHOR_MODULE_DOC, "runtime-footer module documentation"),
        (ANCHOR_IMPORTS_AND_DEFAULTS, "runtime-footer imports/default fields"),
        (ANCHOR_CWD_HELPER, "cwd helper"),
        (ANCHOR_FORMAT_SIGNATURE, "format_runtime_footer signature"),
        (ANCHOR_FORMAT_FIELDS, "format_runtime_footer field branches"),
        (ANCHOR_BUILD_SIGNATURE, "build_footer_line signature"),
        (ANCHOR_BUILD_CALL, "build_footer_line format call"),
    )
    for anchor, label in anchors:
        _count_or_raise(src, anchor, path, label)

    src = src.replace(ANCHOR_MODULE_DOC, REPLACEMENT_MODULE_DOC, 1)
    src = src.replace(ANCHOR_IMPORTS_AND_DEFAULTS, REPLACEMENT_IMPORTS_AND_DEFAULTS, 1)
    src = src.replace(ANCHOR_CWD_HELPER, "", 1)
    src = src.replace(ANCHOR_FORMAT_SIGNATURE, REPLACEMENT_FORMAT_SIGNATURE, 1)
    src = src.replace(ANCHOR_FORMAT_FIELDS, REPLACEMENT_FORMAT_FIELDS, 1)
    src = src.replace(ANCHOR_BUILD_SIGNATURE, REPLACEMENT_BUILD_SIGNATURE, 1)
    src = src.replace(ANCHOR_BUILD_CALL, REPLACEMENT_BUILD_CALL, 1)
    src += (
        f"\n# {APPLIED_MARKER}: replaced static cwd with turn/session cost and duration.\n"
    )

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch: runtime footer now shows turn/session cost and duration in {path}")


def _patch_run() -> None:
    spec = importlib.util.find_spec("gateway.run")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/run.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    anchors = (
        (ANCHOR_RUN_CONVERSATION, "run_conversation cost snapshot"),
        (ANCHOR_SLACK_FOOTER_COMMENT, "Slack footer privacy comment"),
        (ANCHOR_SLACK_TRAILING_MARKER, "Slack footer trailing marker"),
        (ANCHOR_EARLY_RETURN, "early return cost fields"),
        (ANCHOR_FINAL_RETURN, "final return cost fields"),
        (ANCHOR_FOOTER_CALL, "post-0036 footer call"),
    )
    for anchor, label in anchors:
        _count_or_raise(src, anchor, path, label)

    src = src.replace(ANCHOR_RUN_CONVERSATION, REPLACEMENT_RUN_CONVERSATION, 1)
    src = src.replace(
        ANCHOR_SLACK_FOOTER_COMMENT, REPLACEMENT_SLACK_FOOTER_COMMENT, 1
    )
    src = src.replace(
        ANCHOR_SLACK_TRAILING_MARKER, REPLACEMENT_SLACK_TRAILING_MARKER, 1
    )
    src = src.replace(ANCHOR_EARLY_RETURN, REPLACEMENT_EARLY_RETURN, 1)
    src = src.replace(ANCHOR_FINAL_RETURN, REPLACEMENT_FINAL_RETURN, 1)
    src = src.replace(ANCHOR_FOOTER_CALL, REPLACEMENT_FOOTER_CALL, 1)
    src += (
        f"\n# {APPLIED_MARKER}: threaded turn/session cost and duration into the footer.\n"
    )

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch: turn/session cost and duration now threaded through {path}")


def main() -> int:
    _patch_runtime_footer()
    _patch_run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
