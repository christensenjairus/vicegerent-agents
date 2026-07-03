#!/usr/bin/env python3
"""Vicegerent patch: let ``pattern_silence`` also silence execute_code's
whole-script approval gate.

Context
-------
``tools/approval.py`` has two independent approval gates:

  1. ``check_all_command_guards`` — regex-flagged ``terminal()`` commands.
     Patch 0008 already lets ``approvals.pattern_silence`` (read from the
     immutable ``/opt/hermes/approval-policy.yaml`` ConfigMap mount) drop
     known-false-positive warnings here before the aux-LLM/gateway prompt.
  2. ``check_execute_code_guard`` — a *separate* one-shot gate that fires
     for every ``execute_code`` call in gateway/ask contexts (i.e. whenever
     ``HERMES_EXEC_ASK=1`` or the session is a gateway platform like
     Slack). This gate never consulted ``pattern_silence`` at all — adding
     an entry to the ConfigMap list did nothing for it, only for gate #1.

Vicegerent sandboxes are single-tenant, non-privileged, network-locked
(egress-proxy MITM + CiliumNetworkPolicy default-deny), with no Docker
socket and no shared credentials beyond what's already scoped to the
agent's own service account. Arbitrary local script execution inside this
sandbox is the intended capability, not an escalation — the isolation
boundary is the pod/network layer, not this gate. So we let operators
silence the ``execute_code`` gate the same way they silence a
``terminal()`` pattern: add ``execute_code`` (or a longer phrase from its
description, e.g. ``execute_code script execution``) to
``approval-policy.yaml``'s ``pattern_silence`` list.

Design
------
Same anchor style as patch 0008: a case-insensitive substring match of
``pattern_silence`` entries against ``check_execute_code_guard``'s fixed
``description`` string. Inserted immediately after the cron-session
branch (cron's explicit deny/approve stance is authoritative — no user is
present there regardless of silence-list config) and before the
gateway/ask escalation. Docker/yolo/off bypasses above it are untouched.

Fail-loud by design: if the anchor is absent or appears more than once,
the patch raises and the image build fails.
"""
import importlib.util
import sys

ANCHOR = (
    "    # Cron: no user is present to approve arbitrary code.\n"
    "    if env_var_enabled(\"HERMES_CRON_SESSION\"):\n"
    "        if _get_cron_approval_mode() == \"deny\":\n"
    "            return {\n"
    "                \"approved\": False,\n"
    "                \"message\": (\n"
    "                    \"BLOCKED: execute_code runs arbitrary local Python \"\n"
    "                    \"(including subprocess calls that bypass shell-string \"\n"
    "                    \"approval checks). Cron jobs run without a user present \"\n"
    "                    \"to approve it. Use normal tools instead, or set \"\n"
    "                    \"approvals.cron_mode: approve only if this cron profile \"\n"
    "                    \"is intentionally trusted.\"\n"
    "                ),\n"
    "                \"pattern_key\": pattern_key,\n"
    "                \"description\": description,\n"
    "                \"outcome\": \"blocked\",\n"
    "                \"user_consent\": False,\n"
    "            }\n"
    "        return {\"approved\": True, \"message\": None}\n"
    "\n"
    "    # Only gateway/ask contexts get the one-shot whole-script approval.\n"
)

REPLACEMENT = (
    "    # Cron: no user is present to approve arbitrary code.\n"
    "    if env_var_enabled(\"HERMES_CRON_SESSION\"):\n"
    "        if _get_cron_approval_mode() == \"deny\":\n"
    "            return {\n"
    "                \"approved\": False,\n"
    "                \"message\": (\n"
    "                    \"BLOCKED: execute_code runs arbitrary local Python \"\n"
    "                    \"(including subprocess calls that bypass shell-string \"\n"
    "                    \"approval checks). Cron jobs run without a user present \"\n"
    "                    \"to approve it. Use normal tools instead, or set \"\n"
    "                    \"approvals.cron_mode: approve only if this cron profile \"\n"
    "                    \"is intentionally trusted.\"\n"
    "                ),\n"
    "                \"pattern_key\": pattern_key,\n"
    "                \"description\": description,\n"
    "                \"outcome\": \"blocked\",\n"
    "                \"user_consent\": False,\n"
    "            }\n"
    "        return {\"approved\": True, \"message\": None}\n"
    "\n"
    "    # Pattern silence list: let operator-configured substrings in\n"
    "    # /opt/hermes/approval-policy.yaml (hermes-approval-policy ConfigMap,\n"
    "    # immutable at runtime) silence the whole-script execute_code gate,\n"
    "    # same mechanism patch 0008 wires into check_all_command_guards.\n"
    "    # Vicegerent patch: execute_code has its own separate approval gate\n"
    "    # that pattern_silence never reached before this.\n"
    "    def _read_pattern_silence_execute_code():\n"
    "        import pathlib\n"
    "        policy_path = pathlib.Path(\"/opt/hermes/approval-policy.yaml\")\n"
    "        if not policy_path.exists():\n"
    "            return []\n"
    "        try:\n"
    "            import yaml\n"
    "            data = yaml.safe_load(policy_path.read_text(encoding=\"utf-8\")) or {}\n"
    "            return [\n"
    "                s.lower().strip() for s in\n"
    "                data.get(\"pattern_silence\", []) or []\n"
    "                if s\n"
    "            ]\n"
    "        except Exception:\n"
    "            return []\n"
    "    _exec_silence = _read_pattern_silence_execute_code()\n"
    "    if _exec_silence and any(s in description.lower() for s in _exec_silence):\n"
    "        return {\"approved\": True, \"message\": None}\n"
    "\n"
    "    # Only gateway/ask contexts get the one-shot whole-script approval.\n"
)

APPLIED_MARKER = "_read_pattern_silence_execute_code"


def main() -> int:
    spec = importlib.util.find_spec("tools.approval")
    if spec is None or not spec.origin:
        import pathlib
        candidates = list(pathlib.Path("/usr/local/lib/hermes-agent").rglob("approval.py"))
        if not candidates:
            raise SystemExit("patch: cannot locate tools/approval.py")
        path = str(candidates[0])
    else:
        path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {path}, found {count} "
            "(upstream drifted — re-verify check_execute_code_guard)"
        )

    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: execute_code pattern_silence filter applied and compiled OK — {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
