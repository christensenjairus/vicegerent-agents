#!/usr/bin/env python3
"""Stop the ``custom`` provider profile from injecting an unconditional
``extra_body.think = False`` field that hard-400s on schema-strict
OpenAI-compatible endpoints (e.g. gpt-5.4 via agentgateway).

Context
-------
``plugins/model-providers/custom/__init__.py::CustomProfile.build_api_kwargs_extras``
is the code path used for any endpoint registered as ``provider: custom``
(Ollama, vLLM, llama.cpp, GLM/ARK, and any other OpenAI-compatible chat
completions endpoint a user points Hermes at -- including this cluster's
``gpt-5.4`` fallback route through agentgateway). When reasoning is
disabled for the target model, it unconditionally emitted BOTH:

    top_level["reasoning_effort"] = "none"
    extra_body["think"] = False

The OpenAI Python SDK flattens ``extra_body`` into literal top-level JSON
keys on the wire (see ``openai._base_client``: ``extra_body`` becomes
``extra_json`` merged onto the request body, not a nested field). So
``extra_body["think"] = False`` is sent as a bare top-level ``"think":
false`` in the request JSON to WHATEVER the ``custom`` provider's
``base_url`` happens to be.

Ollama's own module docstring already documents that this field is a
no-op on the code path Hermes actually calls: "Ollama's /v1/chat/completions
ignores think=False -- only /api/chat honours it (ollama#14820)". Hermes
never calls Ollama's native ``/api/chat``; it always uses the OpenAI-
compatible ``/v1/chat/completions`` transport. So ``extra_body["think"]``
never worked for its stated purpose (silencing Ollama's chain-of-thought)
on the actual call path -- it was dead weight for its intended target.

Meanwhile, schema-strict OpenAI-compatible endpoints (real OpenAI, and
proxies like agentgateway that pass ``extra_body`` straight through as
literal top-level fields rather than silently dropping unknown keys)
reject the unrecognized ``think`` field outright:

    HTTP 400: Unknown parameter: 'think'.

Live incident (2026-07-22, this deployment): the SAME turn that patch 0037
+ the ``agent.reasoning_overrides.gpt-5.4: none`` config change (MR !604,
merged the same day) were meant to make survivable hit fallback -- and the
fallback itself 400'd, this time on ``think`` instead of the previously
patched ``reasoning_effort``. The ``reasoning_effort`` fix from !604 is
confirmed working (the log shows fallback engaging cleanly on
``reasoning_effort``); ``think`` is a SEPARATE, previously-undiscovered
field emitted by the exact same code path that !604 didn't touch, because
!604 only exercised the ``reasoning_overrides`` config plumbing, not this
provider profile's ``extra_body`` side effect.

Fix
---
Drop the ``extra_body["think"] = False`` assignment entirely. The
top-level ``reasoning_effort = "none"`` alone is sufficient and correct
signal for every backend this profile actually targets (Ollama's
/v1/chat/completions, vLLM, llama.cpp, GLM/ARK, and strict OpenAI-schema
endpoints like gpt-5.4) -- each either respects ``reasoning_effort`` or
silently ignores it, and none of them need (or, for the strict ones,
tolerate) the extra ``think`` field.

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream refactored the profile), the patch raises and
the image build fails, signalling a re-verify. Idempotent: a re-run after
a successful apply is a no-op.

Remove once upstream either (a) makes this provider profile detect real
Ollama endpoints (e.g. via ``agent.model_metadata.is_local_endpoint``) and
scope the ``think`` field to only those, or (b) upstream Ollama makes its
own ``/v1/chat/completions`` honour ``think`` so the field stops being
useless dead weight even for its intended target.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0040"

ANCHOR = (
    "            if _effort == \"none\" or _enabled is False:\n"
    "                # Ollama's /v1/chat/completions silently ignores\n"
    "                # extra_body.think (only /api/chat honours it — ollama#14820)\n"
    "                # but respects the top-level reasoning_effort field, so both\n"
    "                # are needed to actually stop a thinking-capable model from\n"
    "                # reasoning (#25758). Endpoints that recognize neither simply\n"
    "                # ignore them.\n"
    "                top_level[\"reasoning_effort\"] = \"none\"\n"
    "                extra_body[\"think\"] = False\n"
)

REPLACEMENT = (
    f"            # {APPLIED_MARKER}: extra_body.think=False removed. It was a\n"
    "            # no-op for its intended target (Ollama's own docs confirm\n"
    "            # /v1/chat/completions ignores think -- only /api/chat honours\n"
    "            # it, ollama#14820, and Hermes only ever calls the former) while\n"
    "            # hard-400ing (\"Unknown parameter: 'think'\") on schema-strict\n"
    "            # OpenAI-compatible endpoints such as gpt-5.4 via agentgateway.\n"
    "            # top-level reasoning_effort=\"none\" alone is correct and\n"
    "            # sufficient signal for every backend this profile targets.\n"
    "            if _effort == \"none\" or _enabled is False:\n"
    "                top_level[\"reasoning_effort\"] = \"none\"\n"
)


def main() -> int:
    spec = importlib.util.find_spec("plugins.model_providers.custom")
    if spec is None or not spec.origin:
        import pathlib

        candidates = list(
            pathlib.Path("/opt/hermes/plugins/model-providers/custom").glob("__init__.py")
        )
        if not candidates:
            raise SystemExit(
                "patch: cannot locate plugins/model-providers/custom/__init__.py "
                "via importlib or direct path search"
            )
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
            f"patch: expected exactly 1 occurrence of the extra_body['think'] "
            f"anchor in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: custom provider profile no longer emits extra_body['think'] "
        f"in {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
