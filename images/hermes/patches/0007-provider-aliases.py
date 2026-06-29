#!/usr/bin/env python3
"""Vicegerent patch: add agentgateway route-name aliases to resolve_provider().

hermes_cli/auth.py::resolve_provider() has a local _PROVIDER_ALIASES dict.
When provider='sonnet' (our agentgateway route name), it is not in that dict
and not in PROVIDER_REGISTRY, so AuthError is raised. The caller catches this
and falls back to provider='custom', which is then written to billing_provider
in state.db — bypassing all pricing lookups.

This patch injects our agentgateway route-name keys into auth.py's local alias
dict so resolve_provider('sonnet') -> 'anthropic' -> real billing.

Remove once Hermes supports a billing_provider field in the provider config.
"""
import importlib.util
import sys

ALIASES_TO_ADD = {
    # Anthropic-transport agentgateway routes
    "sonnet":      "anthropic",
    "haiku":       "anthropic",
    "opus":        "anthropic",
    "haiku-oai":   "anthropic",
    # OpenAI-transport agentgateway routes (route names use dashes not dots)
    "gpt-5-5":     "openai-api",
    "gpt-4-1":     "openai-api",
    "gpt-4o-mini": "openai-api",
}

# Anchor: end of the local _PROVIDER_ALIASES dict in resolve_provider()
ANCHOR = (
    '        "vllm": "custom", "llamacpp": "custom",\n'
    '        "llama.cpp": "custom", "llama-cpp": "custom",\n'
    '    }\n'
)

REPLACEMENT = (
    '        "vllm": "custom", "llamacpp": "custom",\n'
    '        "llama.cpp": "custom", "llama-cpp": "custom",\n'
    '        # vicegerent: agentgateway route-name -> canonical billing provider\n'
    + "".join(f'        "{k}": "{v}",\n' for k, v in ALIASES_TO_ADD.items())
    + '    }\n'
)

# Marker written into the file — checked here after patching, not in the Dockerfile
# (Dockerfile shell quoting strips embedded double-quotes from -c "..." strings).
APPLIED_MARKER = '"sonnet": "anthropic"'


def main() -> int:
    spec = importlib.util.find_spec("hermes_cli.auth")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli.auth module")
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
            "(Hermes upstream changed auth.py resolve_provider aliases -- re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")

    # Verify the marker is present in the written file -- do this here, not in
    # the Dockerfile, because shell quoting inside -c "..." strips double-quotes.
    with open(path, "r", encoding="utf-8") as f:
        written = f.read()
    if APPLIED_MARKER not in written:
        raise SystemExit(f"patch: marker not found in {path} after write -- bug in patch script")

    print(f"patch: added {len(ALIASES_TO_ADD)} agentgateway aliases to resolve_provider() in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
