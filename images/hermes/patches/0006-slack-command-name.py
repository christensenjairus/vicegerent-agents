"""
Patch: configurable Slack slash command name via HERMES_SLACK_COMMAND_NAME env var.

By default Hermes registers /hermes as its catch-all Slack slash command.
This patch replaces that hardcoded name with the value of
HERMES_SLACK_COMMAND_NAME (default: "hermes"), so the bot can be deployed
as /felix, /aria, or any other name without touching upstream source.

Three sites are patched:
  1. hermes_cli/commands.py :: slack_native_slashes()
     — the first entry in the list (the catch-all slash)
  2. gateway/platforms/slack.py :: SlackAdapter.connect()
     — the fallback regex used when slack_native_slashes() returns empty
  3. gateway/platforms/slack.py :: SlackAdapter._handle_slash_command()
     — the set literal that identifies the catch-all slash at dispatch time

All three read the same env var at import time so they stay consistent.
Remove this patch once upstream Hermes supports HERMES_SLACK_COMMAND_NAME.
"""

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the installed Hermes source tree
# ---------------------------------------------------------------------------

def _find_module_path(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise FileNotFoundError(f"Cannot locate module: {module_name}")
    return Path(spec.origin)


commands_path = _find_module_path("hermes_cli.commands")
slack_path = _find_module_path("gateway.platforms.slack")

print(f"Patching {commands_path}")
print(f"Patching {slack_path}")


# ---------------------------------------------------------------------------
# Helper: apply a single sed-style substitution with strict uniqueness checks
# ---------------------------------------------------------------------------

def _patch(path: Path, old: str, new: str, description: str) -> None:
    src = path.read_text()
    count = src.count(old)
    if count == 0:
        raise RuntimeError(
            f"Patch marker not found in {path}\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    if count > 1:
        raise RuntimeError(
            f"Patch marker is ambiguous in {path} ({count} matches)\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    path.write_text(src.replace(old, new, 1))
    print(f"  ok  {description}")


# ---------------------------------------------------------------------------
# 1. commands.py — replace the hardcoded "hermes" entry in slack_native_slashes()
# ---------------------------------------------------------------------------

_patch(
    commands_path,
    old=(
        '    # Reserve /hermes as the catch-all top-level command.\n'
        '    entries.append(("hermes", "Talk to Hermes or run a subcommand", "[subcommand] [args]"))\n'
        '    seen.add("hermes")'
    ),
    new=(
        '    # Reserve the catch-all top-level command (name from env; default "hermes").\n'
        '    import os as _os\n'
        '    _slack_cmd_name = _os.environ.get("HERMES_SLACK_COMMAND_NAME", "hermes").strip().lower() or "hermes"\n'
        '    entries.append((_slack_cmd_name, f"Talk to {_slack_cmd_name.capitalize()} or run a subcommand", "[subcommand] [args]"))\n'
        '    seen.add(_slack_cmd_name)'
    ),
    description="commands.py: slack_native_slashes() catch-all entry",
)


# ---------------------------------------------------------------------------
# 2. slack.py — fallback regex when slack_native_slashes() returns empty
# ---------------------------------------------------------------------------

_patch(
    slack_path,
    old='                _slash_pattern = _re.compile(r"^/hermes$")',
    new=(
        '                import os as _os\n'
        '                _fb_name = _os.environ.get("HERMES_SLACK_COMMAND_NAME", "hermes").strip().lower() or "hermes"\n'
        '                _slash_pattern = _re.compile(r"^/" + _re.escape(_fb_name) + r"$")'
    ),
    description="slack.py: fallback regex in connect()",
)


# ---------------------------------------------------------------------------
# 3. slack.py — dispatch set in _handle_slash_command()
# ---------------------------------------------------------------------------

_patch(
    slack_path,
    old='        if slash_name in {"hermes", ""}:',
    new=(
        '        import os as _os\n'
        '        _catch_all = _os.environ.get("HERMES_SLACK_COMMAND_NAME", "hermes").strip().lower() or "hermes"\n'
        '        if slash_name in {_catch_all, ""}:'
    ),
    description="slack.py: dispatch set in _handle_slash_command()",
)


# ---------------------------------------------------------------------------
# Smoke-test: import both modules; verify the env var is read
# ---------------------------------------------------------------------------

print("Smoke-testing patched modules...")
os.environ.setdefault("HERMES_SLACK_COMMAND_NAME", "hermes")

# Reload from disk so we exercise the patched source
import importlib
import hermes_cli.commands as _cmd_mod
importlib.reload(_cmd_mod)
slashes = _cmd_mod.slack_native_slashes()
first_name = slashes[0][0] if slashes else None
assert first_name == "hermes", f"Expected 'hermes', got {first_name!r}"
print(f"  ok  slack_native_slashes()[0] = {slashes[0]!r}")

# Verify the env var is actually honoured
os.environ["HERMES_SLACK_COMMAND_NAME"] = "felix"
importlib.reload(_cmd_mod)
slashes2 = _cmd_mod.slack_native_slashes()
first_name2 = slashes2[0][0] if slashes2 else None
assert first_name2 == "felix", f"Expected 'felix', got {first_name2!r}"
print(f"  ok  HERMES_SLACK_COMMAND_NAME=felix → {slashes2[0]!r}")

# Restore to default for subsequent patch verification steps
os.environ["HERMES_SLACK_COMMAND_NAME"] = "hermes"

print("Patch 0006 applied and verified.")
