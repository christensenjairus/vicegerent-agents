#!/usr/bin/env python3
"""Vicegerent patch: stop the Tool Search context-length probe from dialing
openrouter.ai at startup in the egress-locked sandbox.

model_tools.py::_resolve_active_context_length() resolves the active model's
context window to gate Tool Search. Upstream calls get_model_context_length()
with ONLY the model id, so config-based resolution (the explicit
`model.context_length` override and the per-custom-provider override) cannot
fire, and it falls through to the step-6 OpenRouter `/models` HTTP fetch. In an
egress-locked pod that connect is blackholed and retried IPv4+IPv6 (~30s added
to every CLI start).

This patch threads `base_url`, `config_context_length`, and `custom_providers`
into the call so resolution short-circuits at step 0/0b from local config — no
network, and Tool Search keeps working (it just gets a real window to threshold
against). Verified offline: resolves the configured value with all sockets
blocked.

Fail-loud by design: if the upstream anchor line is absent or appears more than
once (i.e. upstream refactored this path), the patch raises and the image build
fails, signalling a re-verify instead of silently shipping the slow probe.

Upstream fix to be filed separately; drop this patch once it lands.
"""
import importlib.util
import sys

ANCHOR = "        return int(get_model_context_length(model_id) or 0)\n"

REPLACEMENT = (
    "        base_url = (model_cfg.get(\"base_url\") or \"\").strip()\n"
    "        cfg_ctx = model_cfg.get(\"context_length\")\n"
    "        try:\n"
    "            from hermes_cli.config import get_compatible_custom_providers as _gccp\n"
    "            _cps = _gccp(cfg)\n"
    "        except Exception:\n"
    "            _cps = None\n"
    "        return int(get_model_context_length(\n"
    "            model_id, base_url=base_url, config_context_length=cfg_ctx,\n"
    "            custom_providers=_cps) or 0)\n"
)

# Idempotence marker: a unique token from the replacement so a re-run is a no-op
# rather than a hard failure (the anchor is gone after the first apply).
APPLIED_MARKER = "custom_providers=_cps) or 0)"


def main() -> int:
    spec = importlib.util.find_spec("model_tools")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate model_tools module")
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
            "(upstream drifted — re-verify the Tool Search context-length path)"
        )

    src = src.replace(ANCHOR, REPLACEMENT)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # Syntax-check the patched module compiles.
    compile(src, path, "exec")
    print(f"patch: applied Tool Search context-length fix to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
