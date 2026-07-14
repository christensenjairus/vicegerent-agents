#!/usr/bin/env python3
"""Vicegerent patch: don't fail setup.runtime_check (and any other
has_usable_secret() consumer) for a real, working Anthropic session just
because the resolved token happens to be our sandbox's placeholder value.

Context
-------
This platform sets ANTHROPIC_API_KEY="none" in the sandbox env (see  # pragma: allowlist secret
_sandbox.tpl) so Claude Code / Codex / Hermes clients have a non-empty
credential string to send -- the real upstream key is injected by
agentgateway and the value never leaves the pod (egress proxy scrubs it
in transit either way). "none" is intentionally chosen because it IS on
hermes_cli.auth._PLACEHOLDER_SECRET_VALUES: that's what stops the
canonical (non-gateway) "anthropic" / "openai-api" providers from
falsely registering as explicitly user-configured and leaking into the
desktop's Edit Models dialog (see this repo's HAH-112 fix commit).

That fix, applied consistently, surfaced a second, independent bug: the
SAME placeholder rejection now also fires inside
hermes_cli.runtime_provider.resolve_runtime_provider()'s native-Anthropic
branch. When model.provider == "anthropic" and model.base_url points at
our agentgateway /anthropic route, _anthropic_base_url_override_ok()
(patch 0014's helper) correctly trusts that base_url and
resolve_anthropic_token() falls through OAuth/Claude-Code sources to
os.getenv("ANTHROPIC_API_KEY") = "none" -- a non-empty string, so the
function's own `if not token: raise AuthError(...)` guard does NOT fire,
and the resolved runtime dict carries api_key="none" out to callers.  # pragma: allowlist secret

tui_gateway/server.py's "setup.runtime_check" RPC (introduced to give
desktop/dashboard UIs a strict "will this actually work" signal) wraps
that api_key through hermes_cli.auth.has_usable_secret() before trusting
it:

    credential_ok = (
        callable(api_key)
        or api_key_text in {"aws-sdk", "no-key-required"}
        or has_usable_secret(api_key_text)
        or bool(runtime.get("command"))
    )

has_usable_secret("none") is (correctly, per its own contract) False, so
credential_ok is False, and the desktop surfaces "No usable credentials
found for anthropic. setup.status reports configured credentials, but
runtime resolution still failed." -- even though real chat turns work
fine, because the actual Anthropic client request is sent to the trusted
agentgateway base_url, which supplies the real key downstream. This is a
readiness-check false negative, not a functional break.

Fix
---
In resolve_runtime_provider()'s native-Anthropic (non-Azure) branch: after
resolving `token`, if the request is routed through a trusted gateway
override (cfg_base_url is set -- which only happens when
_anthropic_base_url_override_ok() already accepted it) AND the token is
not has_usable_secret() (i.e. it's a recognized placeholder, not a real
secret), normalize it to the same "no-key-required" sentinel this file
already uses elsewhere for local/no-auth servers (Ollama, LM Studio,
etc. -- see _resolve_openrouter_runtime and friends). setup.runtime_check
already explicitly allowlists "no-key-required" as credential_ok, so this
one-line normalization fixes the false negative without touching the
gateway/UI code at all.

A genuinely unconfigured Anthropic setup (ANTHROPIC_API_KEY unset, no
OAuth token anywhere) is unaffected: resolve_anthropic_token() returns
None in that case, the existing `if not token: raise AuthError(...)`
still fires first, and this patch's check never runs.

has_usable_secret is already imported at module scope in
runtime_provider.py (patch does not need to add an import).

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream refactored this branch), the patch raises and
the image build fails, signalling a re-verify.

Depends on patch 0014 (needs cfg_base_url / _anthropic_base_url_override_ok
already in place; must run after it). Once upstream itself normalizes
placeholder-shaped-but-gateway-routed Anthropic tokens to "no-key-required"
(or teaches setup.runtime_check that a trusted gateway override doesn't
need a locally-usable secret), this patch can be dropped.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0020"

ANCHOR = (
    "        else:\n"
    "            from agent.anthropic_adapter import resolve_anthropic_token\n"
    "            token = resolve_anthropic_token()\n"
    "            if not token:\n"
    "                raise AuthError(\n"
    "                    \"No Anthropic credentials found. Set ANTHROPIC_TOKEN or ANTHROPIC_API_KEY, \"\n"
    "                    \"run 'claude setup-token', or authenticate with 'claude /login'.\"\n"
    "                )\n"
)

REPLACEMENT = (
    "        else:\n"
    "            from agent.anthropic_adapter import resolve_anthropic_token\n"
    "            token = resolve_anthropic_token()\n"
    "            if not token:\n"
    "                raise AuthError(\n"
    "                    \"No Anthropic credentials found. Set ANTHROPIC_TOKEN or ANTHROPIC_API_KEY, \"\n"
    "                    \"run 'claude setup-token', or authenticate with 'claude /login'.\"\n"
    "                )\n"
    "            # Vicegerent patch 0020: a trusted gateway override (cfg_base_url\n"
    "            # only ever set once _anthropic_base_url_override_ok() already\n"
    "            # accepted it) supplies the real upstream key downstream, so a\n"
    "            # placeholder-shaped token here (e.g. this platform's\n"
    "            # ANTHROPIC_API_KEY=\"none\") is not a missing credential -- normalize\n"
    "            # it to the same no-key-required sentinel used elsewhere in this\n"
    "            # file for local/no-auth servers, so has_usable_secret() consumers\n"
    "            # (setup.runtime_check) don't report a false readiness failure.\n"
    "            if cfg_base_url and not has_usable_secret(token):\n"
    "                token = \"no-key-required\"\n"
)


def _patch_runtime_provider() -> None:
    spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/runtime_provider.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} — no-op")
        return

    count = src.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 native-Anthropic token-resolution anchor "
            f"in {path}, found {count} (upstream drifted — re-verify)"
        )

    src = src.replace(ANCHOR, REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(
        f"patch: gateway-routed Anthropic placeholder tokens now normalize to "
        f"no-key-required in {path}"
    )


def main() -> int:
    _patch_runtime_provider()
    return 0


if __name__ == "__main__":
    sys.exit(main())
