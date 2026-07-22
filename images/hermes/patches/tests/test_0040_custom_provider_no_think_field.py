#!/usr/bin/env python3
"""Behavioral regression test for patch 0040 (custom provider drop
unconditional ``extra_body.think`` field).

Usage: run this INSIDE a Hermes image/container after the patch has been
applied (i.e. against the live installed
plugins/model-providers/custom/__init__.py), or against a scratch copy
during patch development.

    HERMES_CUSTOM_PROVIDER=/path/to/custom/__init__.py python3 \
        test_0040_custom_provider_no_think_field.py

Loads the real module (via importlib, from the live file path — never a
hand-copied snippet, so this test fails loudly if the patch's anchor or
the profile's shape ever drifts) and calls the actual
``CustomProfile.build_api_kwargs_extras`` with the exact reasoning_config
shapes Hermes's runtime produces, asserting the wire-level kwargs match
what every real backend this profile targets can safely accept.

Exit code 0 = all assertions passed. Any failure raises AssertionError
with a descriptive message and exits non-zero.
"""
from __future__ import annotations

import importlib.util
import os
import sys


def _load_custom_profile_module(path: str):
    spec = importlib.util.spec_from_file_location("custom_provider_under_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        raise AssertionError(
            f"failed to import {path} — is this running with /opt/hermes on "
            f"sys.path (providers.register_provider must be importable)? {exc}"
        )
    return module


def main() -> int:
    path = os.environ.get(
        "HERMES_CUSTOM_PROVIDER",
        "/opt/hermes/plugins/model-providers/custom/__init__.py",
    )
    if "/opt/hermes" not in sys.path:
        sys.path.insert(0, "/opt/hermes")

    module = _load_custom_profile_module(path)
    profile = module.custom

    cases: list[tuple[str, dict, dict, dict]] = []

    # --- The exact live-incident shape: reasoning disabled for gpt-5.4 ---
    cases.append((
        "reasoning disabled (live incident shape, e.g. gpt-5.4 via reasoning_overrides)",
        {"enabled": False},
        {},  # extra_body must NOT contain 'think'
        {"reasoning_effort": "none"},  # top_level must be exactly this
    ))

    # --- effort == 'none' string form (same semantic, different config path) ---
    cases.append((
        "reasoning effort explicitly 'none'",
        {"enabled": True, "effort": "none"},
        {},
        {"reasoning_effort": "none"},
    ))

    # --- enabled with an explicit effort level (GLM/ARK style) ---
    cases.append((
        "reasoning enabled with effort='high' (GLM/ARK style)",
        {"enabled": True, "effort": "high"},
        {},
        {"reasoning_effort": "high"},
    ))

    # --- enabled with no effort set: must omit both fields ---
    cases.append((
        "reasoning enabled, no effort set (must omit top-level reasoning_effort)",
        {"enabled": True},
        {},
        {},
    ))

    # --- reasoning_config is None: must be a pure no-op ---
    cases.append((
        "reasoning_config is None (no reasoning-aware model)",
        None,
        {},
        {},
    ))

    failures = []
    for label, reasoning_config, expected_extra, expected_top in cases:
        extra, top = profile.build_api_kwargs_extras(
            reasoning_config=reasoning_config,
            supports_reasoning=reasoning_config is not None,
            model="gpt-5.4",
            base_url=(
                "http://agentgateway-proxy.agentgateway-system"
                ".svc.cluster.local/openai/v1/"
            ),
        )
        extra = extra or {}
        top = top or {}

        ok = True
        if "think" in extra:
            ok = False
        if extra != expected_extra:
            ok = False
        if top != expected_top:
            ok = False

        status = "PASS" if ok else "FAIL"
        print(
            f"[{status}] {label}: extra_body={extra!r} top_level={top!r} "
            f"(expected extra_body={expected_extra!r} top_level={expected_top!r})"
        )
        if not ok:
            failures.append(label)

    if failures:
        raise AssertionError(f"{len(failures)} case(s) failed: {failures}")

    print(f"\nAll {len(cases)} cases passed. No case emits extra_body['think'].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
