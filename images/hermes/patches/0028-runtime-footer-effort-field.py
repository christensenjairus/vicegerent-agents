#!/usr/bin/env python3
"""Vicegerent patch: add an "effort" field to the runtime-metadata footer.

Context
-------
gateway/runtime_footer.py's format_runtime_footer() only recognizes three
field names: "model", "context_pct", "cwd". Any other name in
display.runtime_footer.fields (or its per-platform override) is silently
dropped -- see the function's own trailing comment "Unknown field names are
silently ignored." There is no way to show the current reasoning-effort
level in the footer even though gateway/run.py already tracks it on the
gateway instance (self._reasoning_config, set from
hermes_constants.resolve_reasoning_config() -- see run.py's
_load_reasoning_config()/_resolve_session_reasoning_config()).

Fix
---
Two files, wired together as one patch:

1. gateway/runtime_footer.py: build_footer_line() and format_runtime_footer()
   each gain an `effort: Optional[str] = None` kwarg. format_runtime_footer()
   renders it when the "effort" field is requested and a value is present --
   same silently-skip-when-missing behavior as the other three fields, so a
   footer without effort data omits it cleanly instead of showing an empty/
   placeholder segment.

2. gateway/run.py: both of _run_agent_turn's inner-closure `return` dicts
   (the early-return and the final-return, immediately adjacent to their
   existing "model": _resolved_model / "context_length": _context_length
   keys) gain a new "reasoning_effort" key, sourced from
   self._reasoning_config (already tracked on the gateway instance -- see
   run.py's `self._reasoning_config = reasoning_config` a few lines above
   each return, assigned earlier in the same method invocation). The
   build_footer_line() call site then reads
   agent_result.get("reasoning_effort") and forwards it as `effort=`.

Fail-loud by design: if any anchor is absent or appears an unexpected
number of times (upstream refactored one of these functions), the patch
raises and the image build fails, signalling a re-verify.

Remove once upstream Hermes's runtime footer supports an effort field
itself.
"""
import importlib.util
import sys

# --- gateway/runtime_footer.py anchors -------------------------------------

ANCHOR_FORMAT_SIG = (
    "def format_runtime_footer(\n"
    "    *,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    cwd: Optional[str] = None,\n"
    "    fields: Iterable[str] = _DEFAULT_FIELDS,\n"
    ") -> str:\n"
)

REPLACEMENT_FORMAT_SIG = (
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

ANCHOR_FORMAT_BODY = (
    "        elif field == \"cwd\":\n"
    "            rel = _home_relative_cwd(cwd or os.environ.get(\"TERMINAL_CWD\", \"\"))\n"
    "            if rel:\n"
    "                parts.append(rel)\n"
    "        # Unknown field names are silently ignored.\n"
)

REPLACEMENT_FORMAT_BODY = (
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

ANCHOR_BUILD_SIG = (
    "def build_footer_line(\n"
    "    *,\n"
    "    user_config: dict[str, Any] | None,\n"
    "    platform_key: str | None,\n"
    "    model: Optional[str],\n"
    "    context_tokens: int,\n"
    "    context_length: Optional[int],\n"
    "    cwd: Optional[str] = None,\n"
    ") -> str:\n"
)

REPLACEMENT_BUILD_SIG = (
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

ANCHOR_BUILD_CALL = (
    "    return format_runtime_footer(\n"
    "        model=model,\n"
    "        context_tokens=context_tokens,\n"
    "        context_length=context_length,\n"
    "        cwd=cwd,\n"
    "        fields=cfg.get(\"fields\") or _DEFAULT_FIELDS,\n"
    "    )\n"
)

REPLACEMENT_BUILD_CALL = (
    "    return format_runtime_footer(\n"
    "        model=model,\n"
    "        context_tokens=context_tokens,\n"
    "        context_length=context_length,\n"
    "        cwd=cwd,\n"
    "        effort=effort,\n"
    "        fields=cfg.get(\"fields\") or _DEFAULT_FIELDS,\n"
    "    )\n"
)

# --- gateway/run.py anchors -------------------------------------------------

ANCHOR_RETURN_EARLY = (
    "                    \"model\": _resolved_model,\n"
    "                    \"context_length\": _context_length,\n"
    "                }\n"
    "\n"
    "            # Scan tool results for MEDIA:<path> tags"
)

REPLACEMENT_RETURN_EARLY = (
    "                    \"model\": _resolved_model,\n"
    "                    \"context_length\": _context_length,\n"
    "                    # Vicegerent patch 0028: feeds runtime_footer.py's\n"
    "                    # \"effort\" field.\n"
    "                    \"reasoning_effort\": (\n"
    "                        \"none\"\n"
    "                        if isinstance(self._reasoning_config, dict)\n"
    "                        and self._reasoning_config.get(\"enabled\") is False\n"
    "                        else (\n"
    "                            str((self._reasoning_config or {}).get(\"effort\", \"\") or \"\")\n"
    "                            if isinstance(self._reasoning_config, dict)\n"
    "                            else \"\"\n"
    "                        )\n"
    "                    ),\n"
    "                }\n"
    "\n"
    "            # Scan tool results for MEDIA:<path> tags"
)

ANCHOR_RETURN_FINAL = (
    "                \"model\": _resolved_model,\n"
    "                \"context_length\": _context_length,\n"
    "                \"session_id\": effective_session_id,\n"
    "                \"response_previewed\": result.get(\"response_previewed\", False),\n"
)

REPLACEMENT_RETURN_FINAL = (
    "                \"model\": _resolved_model,\n"
    "                \"context_length\": _context_length,\n"
    "                # Vicegerent patch 0028: feeds runtime_footer.py's \"effort\"\n"
    "                # field.\n"
    "                \"reasoning_effort\": (\n"
    "                    \"none\"\n"
    "                    if isinstance(self._reasoning_config, dict)\n"
    "                    and self._reasoning_config.get(\"enabled\") is False\n"
    "                    else (\n"
    "                        str((self._reasoning_config or {}).get(\"effort\", \"\") or \"\")\n"
    "                        if isinstance(self._reasoning_config, dict)\n"
    "                        else \"\"\n"
    "                    )\n"
    "                ),\n"
    "                \"session_id\": effective_session_id,\n"
    "                \"response_previewed\": result.get(\"response_previewed\", False),\n"
)

ANCHOR_FOOTER_CALL = (
    "                _footer_line = _bfl(\n"
    "                    user_config=_load_gateway_config(),\n"
    "                    platform_key=_platform_config_key(source.platform),\n"
    "                    model=agent_result.get(\"model\"),\n"
    "                    context_tokens=agent_result.get(\"last_prompt_tokens\", 0) or 0,\n"
    "                    context_length=agent_result.get(\"context_length\") or None,\n"
    "                    cwd=os.environ.get(\"TERMINAL_CWD\", \"\"),\n"
    "                )\n"
)

REPLACEMENT_FOOTER_CALL = (
    "                _footer_line = _bfl(\n"
    "                    user_config=_load_gateway_config(),\n"
    "                    platform_key=_platform_config_key(source.platform),\n"
    "                    model=agent_result.get(\"model\"),\n"
    "                    context_tokens=agent_result.get(\"last_prompt_tokens\", 0) or 0,\n"
    "                    context_length=agent_result.get(\"context_length\") or None,\n"
    "                    cwd=os.environ.get(\"TERMINAL_CWD\", \"\"),\n"
    "                    # Vicegerent patch 0028: see runtime_footer.py for the\n"
    "                    # other half of this field.\n"
    "                    effort=agent_result.get(\"reasoning_effort\") or None,\n"
    "                )\n"
)

APPLIED_MARKER = "Vicegerent patch 0028"


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

    _count_or_raise(src, ANCHOR_FORMAT_SIG, path, "format_runtime_footer signature")
    _count_or_raise(src, ANCHOR_FORMAT_BODY, path, "format_runtime_footer cwd/unknown-field body")
    _count_or_raise(src, ANCHOR_BUILD_SIG, path, "build_footer_line signature")
    _count_or_raise(src, ANCHOR_BUILD_CALL, path, "build_footer_line format_runtime_footer call")

    src = src.replace(ANCHOR_FORMAT_SIG, REPLACEMENT_FORMAT_SIG, 1)
    src = src.replace(ANCHOR_FORMAT_BODY, REPLACEMENT_FORMAT_BODY, 1)
    src = src.replace(ANCHOR_BUILD_SIG, REPLACEMENT_BUILD_SIG, 1)
    src = src.replace(ANCHOR_BUILD_CALL, REPLACEMENT_BUILD_CALL, 1)

    src += f"\n# {APPLIED_MARKER}: added an \"effort\" field.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: runtime_footer.py now supports an effort field in {path}")


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

    _count_or_raise(src, ANCHOR_RETURN_EARLY, path, "early-return model/context_length block")
    _count_or_raise(src, ANCHOR_RETURN_FINAL, path, "final-return model/context_length block")
    _count_or_raise(src, ANCHOR_FOOTER_CALL, path, "build_footer_line call")

    src = src.replace(ANCHOR_RETURN_EARLY, REPLACEMENT_RETURN_EARLY, 1)
    src = src.replace(ANCHOR_RETURN_FINAL, REPLACEMENT_RETURN_FINAL, 1)
    src = src.replace(ANCHOR_FOOTER_CALL, REPLACEMENT_FOOTER_CALL, 1)

    src += f"\n# {APPLIED_MARKER}: threaded reasoning_effort into the runtime footer.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: reasoning effort now threaded into runtime footer in {path}")


def main() -> int:
    _patch_runtime_footer()
    _patch_run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
