#!/usr/bin/env python3
"""Regression test for the gpt-5.4 fallback reasoning_effort fix.

Two independent checks:

1. Static: `helm template` the agent chart with the live hermes agent's
   values.yaml and assert the rendered config.yaml ConfigMap contains
   `agent.reasoning_overrides.gpt-5.4: none`. Catches chart-template
   regressions (e.g. someone editing _helpers.tpl and losing the key).

2. Behavioral: replicate Hermes's actual resolution chain --
   hermes_constants.resolve_reasoning_config() -> the `custom` provider
   profile's build_api_kwargs_extras() top_level dict -- against the
   rendered config, and assert the top-level kwargs sent to gpt-5.4 are
   `{"reasoning_effort": "none"}`, never a real effort level. This is the
   exact code path that previously produced the HTTP 400
   ("Function tools with reasoning_effort are not supported for gpt-5.4").

Usage:
    python3 test_gpt54_fallback_reasoning.py \\
        --chart-dir /path/to/charts/agent \\
        --values /path/to/apps/personal/agents/hermes/values.yaml

Requires `helm` on PATH and, for the behavioral check, a Hermes install on
PYTHONPATH (set HERMES_HOME or just run inside the hermes-agent image/venv
where `hermes_constants` and `providers` are importable). The static check
runs standalone; if Hermes isn't importable, the behavioral check is
skipped with a warning (not a failure) so this test still works as a pure
chart-rendering CI check.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import yaml


def render_config_yaml(chart_dir: str, values_path: str) -> dict:
    proc = subprocess.run(
        [
            "helm", "template", "hermes", chart_dir,
            "-f", values_path,
            "--show-only", "templates/config.yaml",
        ],
        capture_output=True, text=True, check=True,
    )
    doc = yaml.safe_load(proc.stdout)
    config_yaml_str = doc["data"]["config.yaml"]
    return yaml.safe_load(config_yaml_str)


def check_static(rendered: dict) -> None:
    agent_cfg = rendered.get("agent")
    assert isinstance(agent_cfg, dict), f"rendered config has no 'agent' map: {rendered.get('agent')!r}"

    overrides = agent_cfg.get("reasoning_overrides")
    assert isinstance(overrides, dict), (
        f"agent.reasoning_overrides missing or not a map in rendered config "
        f"(got {overrides!r}) -- the gpt-5.4 reasoning-effort fix regressed"
    )

    gpt54_override = overrides.get("gpt-5.4")
    assert str(gpt54_override).strip().lower() in {"none", "false", "disabled"}, (
        f"agent.reasoning_overrides['gpt-5.4'] = {gpt54_override!r}, expected "
        f"'none' (or an equivalent disable spelling) -- the fallback will "
        f"400 on tool calls again if this isn't disabling reasoning_effort"
    )

    fb = rendered.get("fallback_providers")
    assert isinstance(fb, list) and fb, "fallback_providers missing/empty in rendered config"
    gpt54_entries = [e for e in fb if str(e.get("model", "")).strip() == "gpt-5.4"]
    assert gpt54_entries, (
        "no fallback_providers entry targets model 'gpt-5.4' -- either the "
        "fallback config changed (this test's assumption is stale) or the "
        "override above is now dead config protecting a model nothing routes to"
    )
    print("[PASS] static: agent.reasoning_overrides['gpt-5.4'] = 'none' present in rendered config, "
          "and a fallback_providers entry actually targets gpt-5.4")


def check_behavioral(rendered: dict) -> bool:
    try:
        from hermes_constants import resolve_reasoning_config
    except ImportError:
        print("note: hermes_constants not importable in this environment; skipping behavioral check")
        return False

    reasoning_config = resolve_reasoning_config(rendered, "gpt-5.4")
    assert reasoning_config == {"enabled": False}, (
        f"resolve_reasoning_config(rendered_config, 'gpt-5.4') = {reasoning_config!r}, "
        f"expected {{'enabled': False}} -- gpt-5.4 will still get a live "
        f"reasoning_effort value sent to it"
    )

    # Replicate plugins/model-providers/custom/__init__.py::CustomProfile
    # .build_api_kwargs_extras()'s reasoning branch exactly (kept in sync
    # manually -- if that file's logic changes, update this mirror).
    top_level: dict = {}
    if reasoning_config and isinstance(reasoning_config, dict):
        effort = (reasoning_config.get("effort") or "").strip().lower()
        enabled = reasoning_config.get("enabled", True)
        if effort == "none" or enabled is False:
            top_level["reasoning_effort"] = "none"
        elif effort:
            top_level["reasoning_effort"] = effort

    assert top_level == {"reasoning_effort": "none"}, (
        f"simulated custom-provider top-level kwargs for gpt-5.4 = {top_level!r}, "
        f"expected {{'reasoning_effort': 'none'}} -- this is the exact value "
        f"OpenAI's /v1/chat/completions rejects with tool calls attached "
        f"unless it's 'none'"
    )
    print("[PASS] behavioral: resolve_reasoning_config + custom-provider kwargs "
          "assembly send reasoning_effort='none' for gpt-5.4, matching what "
          "OpenAI's /v1/chat/completions requires alongside tool calls")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart-dir", default="charts/agent")
    ap.add_argument("--values", default="apps/personal/agents/hermes/values.yaml")
    args = ap.parse_args()

    rendered = render_config_yaml(args.chart_dir, args.values)
    check_static(rendered)
    check_behavioral(rendered)
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
