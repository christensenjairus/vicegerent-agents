#!/usr/bin/env python3
"""Vicegerent patch: add ``approvals.pattern_silence`` to Hermes smart mode.

Context
-------
Hermes ``smart`` mode pre-screens flagged commands with an aux LLM (haiku)
before prompting the user.  The ~50 DANGEROUS_PATTERNS regexes fire on
~500 events per session, almost all false positives (``python3 -c``,
heredocs, ``git push``, ``rm -rf node_modules``, etc.).  Haiku approves
these correctly but costs tokens on every one.

``approvals.pattern_silence`` lets specific pattern descriptions be
silently dropped before haiku sees them, eliminating the token cost for
known false positives in this environment.  The list grows over time as
noise is identified; everything not listed still goes through smart mode.

Design
------
A single anchor is patched into ``check_all_command_guards`` in
``tools/approval.py``, between the warning accumulation phase and the
smart-approval phase::

    gather tirith findings + pattern warnings
      \u2193
    [ANCHOR] silence-list filter (new)
      \u2193
    nothing left? return approved
      \u2193
    smart mode (haiku pre-screens remaining warnings)
      \u2193
    manual prompt if haiku escalates

Filter rules:
  - Tirith findings (is_tirith=True) are NEVER filtered.
  - Patterns whose descriptions match any entry in ``pattern_silence``
    are dropped silently (case-insensitive substring match).
  - Uncancellable patterns (Hermes config/env, system paths, credentials,
    self-termination) are ALWAYS kept, regardless of the silence list.
    These guard the approval gate itself; silencing them would allow
    mid-session escalation to mode=off.

Uncancellable description substrings::
  "hermes"           -- config.yaml/.env escalation + gateway termination
  "system file"      -- tee/redirect to /etc and other sensitive targets
  "system config"    -- /etc/ writes (cp, mv, sed -i)
  "credential"       -- SSH keys, .netrc, .pgpass, .npmrc, .pypirc
  "self-termination" -- kill $(pgrep -f hermes) structural form

Immutability
------------
The silence list is read from /opt/hermes/approval-policy.yaml, which is
mounted read-only from the hermes-approval-policy ConfigMap (NOT from
the writable PVC-backed config.yaml). The agent cannot modify this file
at runtime. To change the list: edit approval-policy.yaml in git, redeploy
the ConfigMap, restart the pod.

Fail-loud by design: if the anchor is absent or appears more than once,
the patch raises and the image build fails.

Remove this patch once upstream Hermes supports pattern_silence natively.
"""
import importlib.util
import sys

# ---------------------------------------------------------------------------
# Anchor \u2014 warning filter insertion point in check_all_command_guards
#
# Inserted between the warning-accumulation phase and the smart-approval
# phase.  Tirith findings are never filtered; uncancellable descriptions
# are always kept; everything else matching the configured silence list
# is silently dropped before haiku is called.
# ---------------------------------------------------------------------------
ANCHOR = (
    "    if is_dangerous:\n"
    "        if not is_approved(session_key, pattern_key):\n"
    "            warnings.append((pattern_key, description, False))\n"
    "\n"
    "    # Nothing to warn about\n"
    "    if not warnings:\n"
    "        return {\"approved\": True, \"message\": None}\n"
)

REPLACEMENT = (
    "    if is_dangerous:\n"
    "        if not is_approved(session_key, pattern_key):\n"
    "            warnings.append((pattern_key, description, False))\n"
    "\n"
    "    # Pattern silence list: drop non-tirith warnings whose descriptions\n"
    "    # match operator-configured substrings read from the immutable\n"
    "    # /opt/hermes/approval-policy.yaml (hermes-approval-policy ConfigMap).\n"
    "    # Tirith findings are never filtered.  Uncancellable patterns that\n"
    "    # guard the approval gate itself cannot be silenced.\n"
    "    def _read_pattern_silence():\n"
    "        # Read from the immutable ConfigMap mount, not from config.yaml.\n"
    "        # The agent cannot write to this path at runtime.\n"
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
    "    _silence = _read_pattern_silence()\n"
    "    if _silence:\n"
    "        _UNCANCELLABLE = (\n"
    "            \"hermes\",            # config.yaml/.env escalation; gateway termination\n"
    "            \"system file\",       # tee/redirect to /etc and sensitive targets\n"
    "            \"system config\",     # /etc/ writes (cp, mv, sed -i)\n"
    "            \"credential\",        # SSH keys, .netrc, .pgpass, .npmrc, .pypirc\n"
    "            \"self-termination\",  # kill $(pgrep -f hermes) structural form\n"
    "        )\n"
    "        warnings = [\n"
    "            (k, d, t) for k, d, t in warnings\n"
    "            if t  # always keep tirith findings\n"
    "            or any(u in d.lower() for u in _UNCANCELLABLE)  # uncancellable\n"
    "            or not any(s in d.lower() for s in _silence)    # not silenced\n"
    "        ]\n"
    "\n"
    "    # Nothing to warn about\n"
    "    if not warnings:\n"
    "        return {\"approved\": True, \"message\": None}\n"
)

APPLIED_MARKER = "_read_pattern_silence"


def main() -> int:
    spec = importlib.util.find_spec("tools.approval")
    if spec is None or not spec.origin:
        spec = importlib.util.find_spec("approval")
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
        print(f"patch: already applied to {path} \u2014 no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {path}, found {count} "
            "(upstream drifted \u2014 re-verify this patch)"
        )

    src = src.replace(ANCHOR, REPLACEMENT)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: pattern_silence filter applied and compiled OK \u2014 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
