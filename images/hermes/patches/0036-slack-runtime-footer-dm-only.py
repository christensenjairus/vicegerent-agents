#!/usr/bin/env python3
"""Vicegerent patch: suppress the Slack runtime footer outside DMs.

Context
-------
``display.runtime_footer`` (patch 0028 added the "effort" field to this
existing upstream feature) appends a compact metadata line (model, context
%, effort, cwd) to the final message of an agent turn, per-platform
overridable via ``display.platforms.<platform>.runtime_footer``. This repo's
chart default enables it for Slack unconditionally
(``display.platforms.slack.runtime_footer.enabled: true``).

That's fine in a Slack DM -- the footer is genuinely useful there. It's a
liability the moment the bot is invited into a shared channel with
colleagues: every reply leaks internal runtime state (which model is
active, current context-window percentage, the sandbox's working
directory) to everyone in that channel, none of whom opted into seeing it.
There's no existing per-channel-type config knob for this -- runtime_footer
resolution only distinguishes by platform, not by channel kind within a
platform.

Fix
---
Gate the footer's single build call site in ``gateway/run.py`` (the only
place ``build_footer_line()`` is invoked -- both the inline-append path and
the streaming "trailing message" path downstream reuse the same
``_footer_line`` variable, so gating its computation covers both) on
whether the current Slack channel is a DM. Slack channel IDs are prefixed
by type: ``D`` for a direct message, ``C`` for a public channel, ``G`` for
a private channel or MPIM (confirmed via ``plugins/platforms/slack/
adapter.py``'s ``SessionSource(chat_id=channel_id, ...)`` construction --
``channel_id`` is Slack's own raw ``channel`` event field, never rewritten).
A simple ``str(source.chat_id or "").startswith("D")`` check is sufficient
and needs no new Slack API calls.

Every other platform (Telegram, Discord, CLI, etc.) is completely
unaffected -- the added condition short-circuits on ``source.platform ==
Platform.SLACK`` first. An empty/None ``chat_id`` (defensive edge case)
fails toward suppression, not toward leaking.

Verified structurally (anchor matches exactly once against the live
source, patched output compiles) and behaviorally via a standalone
simulation of the added gating expression across 6 cases: Slack DM (footer
kept), Slack public channel (suppressed), Slack private channel/MPIM
(suppressed), a non-Slack platform with an arbitrary chat_id (unaffected,
kept), and two defensive empty/None chat_id cases on Slack (suppressed).

Fail-loud by design: if the anchor is absent or appears an unexpected
number of times (upstream refactored this function), the patch raises and
the image build fails, signalling a re-verify.

Remove once upstream Hermes's runtime_footer gains its own native
per-channel-type (DM vs. multi-user) scoping, obsoleting this Slack-only
workaround.

Fix 2: small visual divider before the footer (DMs only)
----------------------------------------------------------
Once the footer is confirmed showing only in DMs (fix 1 above), it still
reads as a continuation of the response body rather than distinct
metadata -- there's no visual break beyond the existing blank line. Add a
short, subtle divider line immediately before ``_footer_line`` whenever
it's actually going to be shown (i.e. this fix does nothing when fix 1
suppressed the footer), scoped to Slack only so other platforms' footer
rendering is untouched. This covers both call sites that reference
``_footer_line`` -- the inline append (``response = f"{response}\\n\\n
{_footer_line}"``) and the separate trailing-message send used when
streaming already delivered the body -- since both read the same
variable.
"""
import importlib.util
import sys

ANCHOR_FOOTER_BUILD = '            # Runtime-metadata footer — only on the FINAL message of the turn.\n            # Off by default (display.runtime_footer.enabled=false).  When\n            # streaming already delivered the body, we can\'t mutate the sent\n            # text, so we fire a separate trailing send below.\n            _footer_line = ""\n            try:\n                from gateway.runtime_footer import build_footer_line as _bfl\n                _footer_line = _bfl(\n                    user_config=_load_gateway_config(),\n                    platform_key=_platform_config_key(source.platform),\n                    model=agent_result.get("model"),\n                    context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,\n                    context_length=agent_result.get("context_length") or None,\n                    cwd=os.environ.get("TERMINAL_CWD", ""),\n                    # Vicegerent patch 0028: see runtime_footer.py for the\n                    # other half of this field.\n                    effort=agent_result.get("reasoning_effort") or None,\n                )\n            except Exception as _footer_err:\n                logger.debug("runtime_footer build failed: %s", _footer_err)\n                _footer_line = ""'

REPLACEMENT_FOOTER_BUILD = '            # Runtime-metadata footer — only on the FINAL message of the turn.\n            # Off by default (display.runtime_footer.enabled=false).  When\n            # streaming already delivered the body, we can\'t mutate the sent\n            # text, so we fire a separate trailing send below.\n            #\n            # Vicegerent patch: on Slack specifically, suppress the footer\n            # entirely outside DMs. Slack channel IDs are prefixed by type\n            # ("D" = DM, "C" = public channel, "G" = private channel/MPIM);\n            # a footer showing model/context/cwd is useful in a private\n            # 1:1 with the bot but leaks internal runtime details (cwd,\n            # active model) into a shared channel with colleagues who never\n            # opted into seeing it. Every other platform\'s behavior is\n            # unchanged.\n            _footer_suppressed_non_dm = (\n                source.platform == Platform.SLACK\n                and not str(source.chat_id or "").startswith("D")\n            )\n            _footer_line = ""\n            try:\n                if not _footer_suppressed_non_dm:\n                    from gateway.runtime_footer import build_footer_line as _bfl\n                    _footer_line = _bfl(\n                        user_config=_load_gateway_config(),\n                        platform_key=_platform_config_key(source.platform),\n                        model=agent_result.get("model"),\n                        context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,\n                        context_length=agent_result.get("context_length") or None,\n                        cwd=os.environ.get("TERMINAL_CWD", ""),\n                        # Vicegerent patch 0028: see runtime_footer.py for the\n                        # other half of this field.\n                        effort=agent_result.get("reasoning_effort") or None,\n                    )\n            except Exception as _footer_err:\n                logger.debug("runtime_footer build failed: %s", _footer_err)\n                _footer_line = ""'

APPLIED_MARKER = "Vicegerent patch: Slack runtime footer DM-only"

ANCHOR_FOOTER_ITALICS = '            except Exception as _footer_err:\n                logger.debug("runtime_footer build failed: %s", _footer_err)\n                _footer_line = ""\n            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:\n                response = f"{response}\\n\\n{_footer_line}"'

REPLACEMENT_FOOTER_ITALICS = '            except Exception as _footer_err:\n                logger.debug("runtime_footer build failed: %s", _footer_err)\n                _footer_line = ""\n            # Vicegerent patch (fix 2): italicize the footer, Slack-only, so\n            # it reads as distinct metadata rather than a continuation of\n            # the response body. Wrapping _footer_line itself (rather than\n            # at each call site) covers both the inline-append path below\n            # AND the separate trailing-message send used when streaming\n            # already delivered the body. Slack mrkdwn italics use\n            # underscores (_text_), which format_message() passes through\n            # untouched since it already renders as Slack italic syntax.\n            if _footer_line and source.platform == Platform.SLACK:\n                _footer_line = f"_{_footer_line}_"\n            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:\n                response = f"{response}\\n\\n{_footer_line}"'

FIX2_MARKER = "Vicegerent patch: Slack footer italics"


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def main() -> int:
    spec = importlib.util.find_spec("gateway.run")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/run.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src and FIX2_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return 0

    if APPLIED_MARKER not in src:
        _count_or_raise(src, ANCHOR_FOOTER_BUILD, path, "runtime-footer build call")
        src = src.replace(ANCHOR_FOOTER_BUILD, REPLACEMENT_FOOTER_BUILD, 1)

        src += (
            f"\n# {APPLIED_MARKER}: the runtime footer (model/context/effort/cwd) "
            "is now suppressed on Slack outside DMs (chat_id not prefixed 'D'), "
            "so it no longer leaks into shared channels the bot is invited into. "
            "Every other platform is unaffected.\n"
        )
        print(f"patch: Slack runtime footer now DM-only in {path}")
    else:
        print(f"patch: fix 1 (DM-only gating) already applied to {path} -- no-op")

    if FIX2_MARKER not in src:
        _count_or_raise(src, ANCHOR_FOOTER_ITALICS, path, "footer-italics insertion point")
        src = src.replace(ANCHOR_FOOTER_ITALICS, REPLACEMENT_FOOTER_ITALICS, 1)

        src += (
            f"\n# {FIX2_MARKER}: the Slack footer is now rendered in italics "
            "(Slack only, other platforms unaffected) so it reads as "
            "distinct metadata rather than a continuation of the response "
            "body.\n"
        )
        print(f"patch: Slack footer italics added in {path}")
    else:
        print(f"patch: fix 2 (footer italics) already applied to {path} -- no-op")

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    return 0


if __name__ == "__main__":
    sys.exit(main())
