#!/usr/bin/env python3
"""Vicegerent patch: preserve requested_provider name in _resolve_named_custom_runtime.

THE ACTUAL ROOT CAUSE of billing_provider='custom' in state.db:

  _resolve_named_custom_runtime() returns {"provider": "custom", ...} for ALL
  named custom providers (sonnet, haiku, opus, etc.). This value propagates:

    runtime["provider"]
    -> cli_agent_setup_mixin.py: self.provider = resolved_provider
    -> conversation_loop.py:     billing_provider=agent.provider   [written to state.db]

  Patches 0006/0007 fix the billing lookup functions, but they are never
  reached because billing_provider is already 'custom' before they run.

THE FIX:

  In the named-custom-provider path of _resolve_named_custom_runtime (the
  _get_named_custom_provider branch, NOT the direct-alias branch), use
  requested_provider as the "provider" value instead of hardcoded "custom".

  This makes agent.provider = "sonnet" -> billing_provider = "sonnet" written
  to state.db -> resolve_billing_route("sonnet") hits the anthropic alias in
  patch 0007 -> real cost written to state.db.

  The direct-alias path (bare provider: custom + explicit base_url) is left
  unchanged at "custom" since there is no billing identity to resolve to.

  is_custom_provider (chat_completion_helpers.py:797) checks agent.provider ==
  "custom" but the field is only declared in a TypedDict and never used in
  actual logic — confirmed safe to change.

Remove once Hermes supports a billing_provider field in the provider config.
"""
import importlib.util
import sys

# The result dict in the named-custom-provider path of _resolve_named_custom_runtime.
# This is the SECOND "provider": "custom" in the function — after _get_named_custom_provider.
ANCHOR = (
    '    result = {\n'
    '        "provider": "custom",\n'
    '        "api_mode": custom_provider.get("api_mode")\n'
)

REPLACEMENT = (
    '    result = {\n'
    '        # vicegerent: use requested_provider (e.g. "sonnet") not "custom"\n'
    '        # so agent.provider carries the route name through to billing_provider\n'
    '        # in state.db, where patch 0007 can resolve it to the upstream billing\n'
    '        # identity. The direct-alias path above stays "custom" (no identity).\n'
    '        "provider": requested_provider,\n'
    '        "api_mode": custom_provider.get("api_mode")\n'
)

APPLIED_MARKER = '# vicegerent: use requested_provider (e.g. "sonnet") not "custom"'


def main() -> int:
    spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli.runtime_provider module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {path}, found {count} "
            "(Hermes upstream changed _resolve_named_custom_runtime -- re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")

    with open(path, "r", encoding="utf-8") as f:
        written = f.read()
    if APPLIED_MARKER not in written:
        raise SystemExit(f"patch: marker not found in {path} after write -- bug in patch script")

    print(f"patch: _resolve_named_custom_runtime now returns requested_provider for billing in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
