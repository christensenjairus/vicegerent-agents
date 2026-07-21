#!/usr/bin/env python3
"""Vicegerent patch: let display.platforms.<platform> override
file_mutation_verifier, memory_notifications, and busy_ack_enabled.

Context
-------
All three settings are read as flat global-only lookups today, unlike
every other per-platform display toggle in this codebase (tool_progress,
interim_assistant_messages, long_running_notifications, runtime_footer,
show_reasoning, busy_ack_detail, ...), all of which go through
gateway/display_config.py's resolve_display_setting() and therefore honor
display.platforms.<platform>.<key> overrides:

  - run_agent.py's AIAgent._file_mutation_verifier_enabled() reads only
    display.file_mutation_verifier (plus an HERMES_FILE_MUTATION_VERIFIER
    env var) -- no per-platform branch at all.
  - gateway/run.py's per-turn agent-setup block reads only
    display.memory_notifications via a bare dict .get() -- same gap.
  - gateway/run.py's busy-ack path (the code that sends "⚡ Interrupting
    current task..." / "⏳ Queued for the next turn..." etc. when a user
    messages a busy session) reads only the HERMES_GATEWAY_BUSY_ACK_ENABLED
    env var (default "true") -- no config.yaml key, no per-platform branch,
    even though busy_ack_detail (controlling verbosity of the SAME
    message, a few lines below in the same function) already goes through
    resolve_display_setting() correctly.

Confirmed live: setting display.platforms.slack.file_mutation_verifier,
.memory_notifications, or .busy_ack_enabled in config.yaml today has zero
effect: none of the three read sites ever look under display.platforms.

Fix
---
1. run_agent.py: _file_mutation_verifier_enabled() first checks
   display.platforms.<agent.platform>.file_mutation_verifier (when
   agent.platform is set, e.g. "slack" -- see agent/agent_init.py's
   `agent.platform = platform`, itself set from
   gateway/run.py's `_platform_config_key(source.platform)`, so the string
   already matches the same "slack"/"telegram"/"cli" keys used everywhere
   else) before falling through to the existing global
   display.file_mutation_verifier lookup. The HERMES_FILE_MUTATION_VERIFIER
   env var still takes priority over both (unchanged).

2. gateway/run.py: the memory_notifications assignment now calls
   gateway.display_config.resolve_display_setting() with the same
   platform_key used everywhere else in this function (source.platform via
   _platform_config_key), instead of a bare global dict .get(). Falls back
   to "on" exactly as before when nothing is configured at any level.

3. gateway/run.py: the busy_ack_enabled check now also consults
   display.platforms.<platform>.busy_ack_enabled / display.busy_ack_enabled
   via resolve_display_setting() -- using the same `platform_key` variable
   already computed two lines below the read site's original position
   (moved the `platform_key = _platform_config_key(...)` assignment above
   this check so it's in scope). The HERMES_GATEWAY_BUSY_ACK_ENABLED env
   var still takes priority when explicitly set (unchanged, so existing
   service-manager overrides keep working); when unset, the config value
   is consulted before falling back to the prior hardcoded "true" default.

All three changes are purely additive precedence layers -- when no
per-platform override is set anywhere, behavior is byte-for-byte identical
to before.

Fail-loud by design: if any anchor is absent or appears an unexpected
number of times (upstream refactored one of these functions), the patch
raises and the image build fails, signalling a re-verify.

Remove once upstream Hermes's file_mutation_verifier, memory_notifications,
and busy_ack_enabled settings route through resolve_display_setting()
themselves.
"""
import importlib.util
import sys

# --- run_agent.py: _file_mutation_verifier_enabled ------------------------

ANCHOR_VERIFIER = (
    "        try:\n"
    "            import os as _os\n"
    "            env = _os.environ.get(\"HERMES_FILE_MUTATION_VERIFIER\")\n"
    "            if env is not None:\n"
    "                return env.strip().lower() not in {\"0\", \"false\", \"no\", \"off\"}\n"
    "            # Read from the persisted config.yaml so gateway and CLI share\n"
    "            # the same setting.  Import lazily to avoid a startup-time cycle.\n"
    "            try:\n"
    "                from hermes_cli.config import load_config as _load_config\n"
    "                _cfg = _load_config() or {}\n"
    "            except Exception:\n"
    "                _cfg = {}\n"
    "            _display = _cfg.get(\"display\") if isinstance(_cfg, dict) else None\n"
    "            if isinstance(_display, dict) and \"file_mutation_verifier\" in _display:\n"
    "                return bool(_display.get(\"file_mutation_verifier\"))\n"
    "        except Exception:\n"
    "            pass\n"
    "        return True  # safe default: verifier on\n"
)

REPLACEMENT_VERIFIER = (
    "        try:\n"
    "            import os as _os\n"
    "            env = _os.environ.get(\"HERMES_FILE_MUTATION_VERIFIER\")\n"
    "            if env is not None:\n"
    "                return env.strip().lower() not in {\"0\", \"false\", \"no\", \"off\"}\n"
    "            # Read from the persisted config.yaml so gateway and CLI share\n"
    "            # the same setting.  Import lazily to avoid a startup-time cycle.\n"
    "            try:\n"
    "                from hermes_cli.config import load_config as _load_config\n"
    "                _cfg = _load_config() or {}\n"
    "            except Exception:\n"
    "                _cfg = {}\n"
    "            # Vicegerent patch 0030: per-platform override\n"
    "            # (display.platforms.<platform>.file_mutation_verifier) takes\n"
    "            # priority over the global display.file_mutation_verifier,\n"
    "            # mirroring every other per-platform display toggle in this\n"
    "            # codebase (see gateway/display_config.py).\n"
    "            _plat = getattr(self, \"platform\", None)\n"
    "            if _plat and isinstance(_cfg, dict):\n"
    "                _plat_display = (_cfg.get(\"display\") or {}).get(\"platforms\") or {}\n"
    "                _plat_overrides = _plat_display.get(_plat)\n"
    "                if isinstance(_plat_overrides, dict) and \"file_mutation_verifier\" in _plat_overrides:\n"
    "                    return bool(_plat_overrides.get(\"file_mutation_verifier\"))\n"
    "            _display = _cfg.get(\"display\") if isinstance(_cfg, dict) else None\n"
    "            if isinstance(_display, dict) and \"file_mutation_verifier\" in _display:\n"
    "                return bool(_display.get(\"file_mutation_verifier\"))\n"
    "        except Exception:\n"
    "            pass\n"
    "        return True  # safe default: verifier on\n"
)

# --- gateway/run.py: memory_notifications assignment -----------------------

ANCHOR_MEM_NOTIF = (
    "            # Memory update notifications in chat.  Config: display.memory_notifications\n"
    "            #   off     — no chat notification (still logged to stdout)\n"
    "            #   on      — generic \"💾 Memory updated\" (default)\n"
    "            #   verbose — content preview: \"💾 Memory ➕ Hermes Repo...\"\n"
    "            _mem_notif = user_config.get(\"display\", {}).get(\"memory_notifications\")\n"
    "            if isinstance(_mem_notif, bool):\n"
    "                _mem_notif = \"on\" if _mem_notif else \"off\"\n"
    "            agent.memory_notifications = str(_mem_notif).lower() if _mem_notif else \"on\"\n"
)

REPLACEMENT_MEM_NOTIF = (
    "            # Memory update notifications in chat.  Config: display.memory_notifications\n"
    "            #   off     — no chat notification (still logged to stdout)\n"
    "            #   on      — generic \"💾 Memory updated\" (default)\n"
    "            #   verbose — content preview: \"💾 Memory ➕ Hermes Repo...\"\n"
    "            # Vicegerent patch 0030: route through resolve_display_setting() so\n"
    "            # display.platforms.<platform>.memory_notifications overrides the\n"
    "            # global display.memory_notifications, mirroring every other\n"
    "            # per-platform display toggle in this codebase.\n"
    "            from gateway.display_config import resolve_display_setting as _resolve_mem_notif\n"
    "            _mem_notif = _resolve_mem_notif(\n"
    "                user_config, _platform_config_key(source.platform), \"memory_notifications\", None,\n"
    "            )\n"
    "            if isinstance(_mem_notif, bool):\n"
    "                _mem_notif = \"on\" if _mem_notif else \"off\"\n"
    "            agent.memory_notifications = str(_mem_notif).lower() if _mem_notif else \"on\"\n"
)

# --- gateway/run.py: busy_ack_enabled check ---------------------------------

ANCHOR_BUSY_ACK = (
    "        # Check if busy ack is disabled — skip sending but still process the input.\n"
    "        # Placed before debounce so we don't stamp a \"last ack\" timestamp that was\n"
    "        # never actually delivered.\n"
    "        busy_ack_enabled = os.environ.get(\"HERMES_GATEWAY_BUSY_ACK_ENABLED\", \"true\").lower() == \"true\"\n"
    "        if not busy_ack_enabled:\n"
    "            logger.debug(\"Busy ack suppressed for session %s\", session_key)\n"
    "            return True  # input still processed, just no ack sent\n"
    "\n"
    "        # Debounce before consulting config-heavy display settings. Rapid\n"
    "        # follow-ups should be processed but should not trigger another config\n"
    "        # read just to discover that no ack will be sent.\n"
    "        _BUSY_ACK_COOLDOWN = 30\n"
    "        now = time.time()\n"
    "        last_ack = self._busy_ack_ts.get(session_key, 0)\n"
    "        if now - last_ack < _BUSY_ACK_COOLDOWN:\n"
    "            return True  # interrupt sent (if not queue), ack already delivered recently\n"
    "\n"
    "        from gateway.display_config import resolve_display_setting\n"
    "        platform_key = _platform_config_key(event.source.platform)\n"
)

REPLACEMENT_BUSY_ACK = (
    "        # Vicegerent patch 0030: platform_key and resolve_display_setting are\n"
    "        # needed by the busy_ack_enabled check below, so both are pulled up\n"
    "        # ahead of their original position (previously computed just below\n"
    "        # the debounce block, after this check already returned).\n"
    "        from gateway.display_config import resolve_display_setting\n"
    "        platform_key = _platform_config_key(event.source.platform)\n"
    "\n"
    "        # Check if busy ack is disabled — skip sending but still process the input.\n"
    "        # Placed before debounce so we don't stamp a \"last ack\" timestamp that was\n"
    "        # never actually delivered.\n"
    "        #\n"
    "        # Vicegerent patch 0030: the env var still takes priority when explicitly\n"
    "        # set (unchanged, so existing service-manager overrides keep working);\n"
    "        # when unset, display.platforms.<platform>.busy_ack_enabled /\n"
    "        # display.busy_ack_enabled is now consulted before falling back to the\n"
    "        # prior hardcoded \"true\" default, mirroring every other per-platform\n"
    "        # display toggle in this codebase.\n"
    "        _busy_ack_env = os.environ.get(\"HERMES_GATEWAY_BUSY_ACK_ENABLED\")\n"
    "        if _busy_ack_env is not None:\n"
    "            busy_ack_enabled = _busy_ack_env.strip().lower() == \"true\"\n"
    "        else:\n"
    "            _busy_ack_cfg = resolve_display_setting(\n"
    "                _load_gateway_config(), platform_key, \"busy_ack_enabled\", True,\n"
    "            )\n"
    "            # Vicegerent patch 0030: busy_ack_enabled isn't in\n"
    "            # display_config.py's _normalise() bool-coercion set (it's a\n"
    "            # brand-new key upstream doesn't define), so a quoted YAML\n"
    "            # string value (e.g. \"off\") would pass through resolve_\n"
    "            # display_setting() unmodified — and bool(\"off\") is True,\n"
    "            # the opposite of intent. Normalize explicitly here instead of\n"
    "            # trusting a bare bool() coercion.\n"
    "            if isinstance(_busy_ack_cfg, str):\n"
    "                busy_ack_enabled = _busy_ack_cfg.strip().lower() not in {\"0\", \"false\", \"no\", \"off\"}\n"
    "            else:\n"
    "                busy_ack_enabled = bool(_busy_ack_cfg)\n"
    "        if not busy_ack_enabled:\n"
    "            logger.debug(\"Busy ack suppressed for session %s\", session_key)\n"
    "            return True  # input still processed, just no ack sent\n"
    "\n"
    "        # Debounce before consulting config-heavy display settings. Rapid\n"
    "        # follow-ups should be processed but should not trigger another config\n"
    "        # read just to discover that no ack will be sent.\n"
    "        _BUSY_ACK_COOLDOWN = 30\n"
    "        now = time.time()\n"
    "        last_ack = self._busy_ack_ts.get(session_key, 0)\n"
    "        if now - last_ack < _BUSY_ACK_COOLDOWN:\n"
    "            return True  # interrupt sent (if not queue), ack already delivered recently\n"
)

APPLIED_MARKER = "Vicegerent patch 0030"


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _patch_run_agent() -> None:
    spec = importlib.util.find_spec("run_agent")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate run_agent.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_VERIFIER, path, "_file_mutation_verifier_enabled body")
    src = src.replace(ANCHOR_VERIFIER, REPLACEMENT_VERIFIER, 1)
    src += f"\n# {APPLIED_MARKER}: added per-platform file_mutation_verifier override.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: file_mutation_verifier now supports per-platform override in {path}")


def _patch_gateway_run() -> None:
    spec = importlib.util.find_spec("gateway.run")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/run.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_MEM_NOTIF, path, "memory_notifications assignment")
    src = src.replace(ANCHOR_MEM_NOTIF, REPLACEMENT_MEM_NOTIF, 1)

    _count_or_raise(src, ANCHOR_BUSY_ACK, path, "busy_ack_enabled check")
    src = src.replace(ANCHOR_BUSY_ACK, REPLACEMENT_BUSY_ACK, 1)

    src += (
        f"\n# {APPLIED_MARKER}: added per-platform memory_notifications and "
        "busy_ack_enabled overrides.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: memory_notifications and busy_ack_enabled now support "
        f"per-platform override in {path}"
    )


def main() -> int:
    _patch_run_agent()
    _patch_gateway_run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
