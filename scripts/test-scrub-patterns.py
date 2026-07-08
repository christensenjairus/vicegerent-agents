#!/usr/bin/env python3
"""Unit-test the egress-proxy scrub.py secret-redaction regex registry.

scrub.py is embedded as a ConfigMap block-scalar inside a Helm template
(charts/egress-proxy/templates/addon-configmap.yaml), not a normal importable
Python module, so this test renders the ConfigMap with `helm template` (same path
scripts/validate.sh uses), stubs the `mitmproxy` import, exec()s the rendered
source, and asserts against the REAL compiled REDACT_PATTERNS — not a hand-copied
duplicate that could silently drift from what ships.

It covers the regex layer only. The gitleaks second layer lives in the Go sidecar
(images/egress-gitleaks-sidecar) and is unit-tested there with `go test`; here we
monkeypatch _redact_gitleaks to a no-op so _redact's two-layer plumbing (and its
fail-open contract when the sidecar is absent) is exercised without a live sidecar.

Fake/synthetic fixtures only, built by concatenation so no literal credential
string sits verbatim in this file (keeps detect-secrets from flagging the test).

Usage:
  python3 scripts/test-scrub-patterns.py
  SCRUB_PY=/path/to/rendered_scrub.py python3 scripts/test-scrub-patterns.py
"""
import os
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(REPO, "charts", "egress-proxy")
VALUES = os.environ.get("EGRESS_VALUES", os.path.join(REPO, "apps", "work", "egress-proxy", "values.yaml"))
VARS = os.environ.get("CLUSTER_VARS", os.path.join(REPO, "clusters", "work", "cluster-vars.yaml"))

R = "[REDACTED]"


def _need(tool):
    from shutil import which
    if which(tool) is None:
        print(f"SKIP - {tool} not installed; cannot render scrub.py", file=sys.stderr)
        sys.exit(0)


def render_scrub_py():
    """Resolve Flux ${vars} into the overlay values, helm-template the ConfigMap,
    and return the scrub.py source string."""
    _need("helm")
    _need("yq")
    import json
    import re

    data = json.loads(subprocess.check_output(["yq", "-o=json", ".data", VARS]))
    src = open(VALUES).read()
    for k, v in data.items():
        src = src.replace("${" + k + "}", v)
    left = sorted(set(re.findall(r"\$\{[A-Za-z0-9_]+\}", src)))
    if left:
        print(f"ERROR - unresolved cluster-vars tokens in values: {left}", file=sys.stderr)
        sys.exit(1)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(src)
        resolved = f.name
    try:
        rendered = subprocess.check_output(
            ["helm", "template", "egress-proxy", CHART, "-f", resolved,
             "--show-only", "templates/addon-configmap.yaml"]
        )
    finally:
        os.unlink(resolved)
    scrub = subprocess.run(
        ["yq", '.data."scrub.py"'], input=rendered, stdout=subprocess.PIPE, check=True
    ).stdout.decode()
    return scrub


def load_scrub(source):
    """exec the scrub.py source with mitmproxy stubbed; return its namespace."""
    mitm = types.ModuleType("mitmproxy")
    http = types.ModuleType("mitmproxy.http")
    http.HTTPFlow = object
    http.Response = object
    mitm.http = http
    sys.modules["mitmproxy"] = mitm
    sys.modules["mitmproxy.http"] = http
    ns = {}
    exec(compile(source, "scrub.py", "exec"), ns)
    return ns


# Fake, secret-SHAPED fixtures (built by concatenation). pragma: allowlist secret
def fixtures():
    from re import compile as _c  # noqa: F401 (kept parallel to scrub.py imports)
    return [
        ("ssh_private_key",
         "-----BEGIN " + "OPENSSH " + "PRIVATE" + " KEY-----\n"  # pragma: allowlist secret
         + "b3BlbnNzaC1rZXktdjEAAAAA" + "\n-----END " + "OPENSSH " + "PRIVATE" + " KEY-----"),
        ("slack_bot", "xox" + "b-" + "1" * 10 + "-" + "2" * 10 + "-" + "a" * 24),        # pragma: allowlist secret
        ("slack_app", "xapp-" + "1-" + "A" * 10 + "-" + "9" * 20),                       # pragma: allowlist secret
        ("bearer", "Bear" + "er " + "z" * 20 + "." + "y" * 20 + "." + "x" * 10),         # pragma: allowlist secret
        ("basic", "Bas" + "ic " + "b" * 24 + "=="),                                       # pragma: allowlist secret
        ("aws", "AKIA" + "Q" * 16),                                                       # pragma: allowlist secret
        ("github", "gh" + "p_" + "g" * 36),                                               # pragma: allowlist secret
        ("gitlab", "glp" + "at-" + "l" * 20),                                             # pragma: allowlist secret
        ("google", "AIza" + "G" * 35),                                                    # pragma: allowlist secret
        ("openai", "sk-" + "o" * 20),                                                     # pragma: allowlist secret
        ("openai_proj", "sk-" + "proj-" + "p" * 20),                                      # pragma: allowlist secret
        ("anthropic", "sk-" + "ant-" + "n" * 20),                                         # pragma: allowlist secret
        ("stripe", "sk" + "_live_" + "s" * 16),                                           # pragma: allowlist secret
        ("notion", "ntn" + "_" + "t" * 20),                                               # pragma: allowlist secret
        ("twilio", "SK" + "f" * 32),                                                      # pragma: allowlist secret
        ("npm", "npm" + "_" + "m" * 36),                                                  # pragma: allowlist secret
        ("jwt", "eyJ" + "h" * 10 + "." + "eyJ" + "p" * 10 + "." + "s" * 10),              # pragma: allowlist secret
    ]


def apply_regex(patterns, text):
    total = 0
    for pat in patterns:
        text, n = pat.subn(R, text)
        total += n
    return text, total


def main():
    source = os.environ.get("SCRUB_PY")
    source = open(source).read() if source else render_scrub_py()
    ns = load_scrub(source)

    patterns = ns["REDACT_PATTERNS"]
    failures = 0

    # 1. Every fixture must be caught by the regex registry.
    for name, secret in fixtures():
        out, n = apply_regex(patterns, "prefix " + secret + " suffix")
        if n == 0 or secret in out:
            print(f"  FAIL {name}: not redacted -> {out!r}")
            failures += 1
        else:
            print(f"  ok   {name}")

    # 2. Ordinary text must be left untouched (no over-redaction).
    for clean in ("This PR closes the auth bug, no credentials involved.",
                  "GET /api/v1/users?page=2&sort=name",
                  "temperature 0.7, max_tokens 4096"):
        _, n = apply_regex(patterns, clean)
        if n != 0:
            print(f"  FAIL clean text over-redacted ({n}): {clean!r}")
            failures += 1
    print("  ok   clean text untouched" if failures == 0 else "")

    # 3. Two-layer _redact must FAIL OPEN when the gitleaks sidecar is absent.
    #    No sidecar is running in this test, so calling the shipped _redact hits a
    #    connection-refused on 127.0.0.1:8081; _redact_gitleaks must swallow it and
    #    _redact must still return the regex layer's redaction (secret gone, count
    #    >= 1, no exception) rather than raising or blocking.
    secret = "AKIA" + "Q" * 16  # pragma: allowlist secret
    try:
        out, n = ns["_redact"]("key=" + secret)
    except Exception as e:  # pragma: no cover
        print(f"  FAIL _redact raised instead of failing open: {e}")
        failures += 1
    else:
        if n < 1 or secret in out:
            print(f"  FAIL _redact fail-open regression: count={n} out={out!r}")
            failures += 1
        else:
            print("  ok   _redact fails open to regex-only when sidecar unreachable")

    if failures:
        print(f"\nFAIL: {failures} scrub-pattern assertion(s) failed", file=sys.stderr)
        sys.exit(1)
    print("\nPASS: scrub.py regex registry + fail-open plumbing verified")


if __name__ == "__main__":
    main()
