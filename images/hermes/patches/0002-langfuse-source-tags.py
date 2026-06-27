#!/usr/bin/env python3
"""Vicegerent patch: add source:hermes-sdk tag to Langfuse plugin traces.

The built-in observability/langfuse plugin hardcodes tags=["hermes", "langfuse"]
on every trace. This patch replaces that with _build_trace_tags() that:
  - Always includes "source:hermes-sdk" (distinguishes from agentgateway OTel)
  - Adds "sandbox:<env>" when HERMES_LANGFUSE_ENV is set (pod-level identifier)
  - Merges any extra comma-separated tags from HERMES_LANGFUSE_TAGS env var

Filter in Langfuse:
  name = "Hermes turn"               → all Hermes SDK traces
  tags includes "source:hermes-sdk"  → same filter, explicit
  tags includes "source:agentgateway-otel" → gateway OTel spans (set in tracing-policy.yaml)

Fail-loud: if the anchor appears 0 or >1 times the build fails, signalling
upstream refactor rather than silently shipping the wrong tags.
"""
import pathlib
import sys

PLUGIN_PATH = pathlib.Path("/opt/hermes/plugins/observability/langfuse/__init__.py")

# The exact string being replaced (appears exactly once in the plugin source).
ANCHOR = '            tags=["hermes", "langfuse"],\n'

REPLACEMENT = '            tags=_build_trace_tags(),\n'

# Helper inserted immediately before _start_root_trace so it can call _env().
HELPER_ANCHOR = "\ndef _start_root_trace("

HELPER = '''
def _build_trace_tags() -> list[str]:
    """Build Langfuse trace tags.

    Always emits ``source:hermes-sdk`` so SDK-originated traces are
    distinguishable from agentgateway OTel spans in the Langfuse trace list.
    Adds ``sandbox:<env>`` when ``HERMES_LANGFUSE_ENV`` is set (it is set to
    the pod name via the Kubernetes downward API in sandbox.yaml).  Merges any
    extra comma-separated tags from the ``HERMES_LANGFUSE_TAGS`` env var.
    """
    tags = ["source:hermes-sdk"]
    env = _env("HERMES_LANGFUSE_ENV")
    if env:
        tags.append(f"sandbox:{env}")
    extra = _env("HERMES_LANGFUSE_TAGS")
    if extra:
        tags.extend(t.strip() for t in extra.split(",") if t.strip())
    return tags

'''

# Idempotence marker: present in src once the patch is applied.
APPLIED_MARKER = "_build_trace_tags()"


def main() -> int:
    if not PLUGIN_PATH.exists():
        raise SystemExit(f"patch: plugin not found at {PLUGIN_PATH}")

    src = PLUGIN_PATH.read_text(encoding="utf-8")

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {PLUGIN_PATH} \u2014 no-op")
        return 0

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 anchor in {PLUGIN_PATH}, found {count} "
            "(upstream plugin refactored \u2014 re-verify the tags injection path)"
        )

    if HELPER_ANCHOR not in src:
        raise SystemExit(
            f"patch: insertion point '{HELPER_ANCHOR.strip()}' not found in "
            f"{PLUGIN_PATH} \u2014 upstream refactored _start_root_trace signature"
        )

    # Insert the helper immediately before _start_root_trace.
    src = src.replace(HELPER_ANCHOR, HELPER + "\ndef _start_root_trace(", 1)
    # Replace the hardcoded tags list with the helper call.
    src = src.replace(ANCHOR, REPLACEMENT)

    PLUGIN_PATH.write_text(src, encoding="utf-8")
    compile(src, str(PLUGIN_PATH), "exec")
    print(f"patch: applied Langfuse source tags to {PLUGIN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
