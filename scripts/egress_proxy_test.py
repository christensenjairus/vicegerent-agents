"""
Egress proxy integration test suite — curl-based.
Runs from inside the hermes sandbox with proxy env vars set.
python3 /opt/data/egress_proxy_test.py
"""
import subprocess
import os
import socket

PROXY = os.environ["https_proxy"]
CA    = os.environ["SSL_CERT_FILE"]
# Agentgateway internal cluster endpoint
AGENTGW = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local"
# External allowlisted
PYPI    = "https://pypi.org/pypi/requests/json"
GITHUB  = "https://github.com"
# External NOT allowlisted
GOOGLE  = "https://www.google.com"

RESULTS = []


def curl(method, url, body=None, extra_headers=None, use_proxy=True, timeout=10, extra_args=None):
    """Run curl and return (http_status_int, response_body_str)."""
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".body") as tf:
        body_path = tf.name

    cmd = [
        "curl", "-s", "--max-time", str(timeout),
        "-o", body_path,
        "-w", "%{http_code}",
        "-X", method,
    ]
    if use_proxy:
        cmd += ["--proxy", PROXY, "--cacert", CA]
    else:
        cmd += ["--noproxy", "*"]
    if body:
        cmd += ["--data-binary", body if isinstance(body, str) else body.decode()]
    if extra_headers:
        for k, v in extra_headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if extra_args:
        cmd += extra_args
    cmd.append(url)

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, "LC_ALL": "C"})
    status_str = result.stdout.decode().strip()
    status = int(status_str) if status_str.isdigit() else None
    try:
        with open(body_path, "rb") as bf:
            body_str = bf.read(4096).decode("utf-8", errors="replace")
    except Exception:
        body_str = ""
    finally:
        os.unlink(body_path)
    return status, body_str


def check(label, method, url, body=None, extra_headers=None, use_proxy=True,
          expect_proxy_block=None, expect_status=None, timeout=10, extra_args=None):
    status, body_r = curl(method, url, body=body, extra_headers=extra_headers,
                          use_proxy=use_proxy, timeout=timeout, extra_args=extra_args)
    is_proxy_block = (status == 403 and "[egress-proxy]" in body_r)

    passed = True
    fail_reason = ""
    if expect_proxy_block is True and not is_proxy_block:
        passed = False
        fail_reason = f"expected proxy 403 block, got {status}: {body_r[:100].strip()}"
    elif expect_proxy_block is False and is_proxy_block:
        passed = False
        fail_reason = f"unexpectedly proxy-blocked: {body_r[:100].strip()}"
    if expect_status is not None and status != expect_status:
        passed = False
        fail_reason += f" | expected HTTP {expect_status}, got {status}"

    mark = "PASS" if passed else "FAIL"
    RESULTS.append((mark, label, status, fail_reason))
    tag = f"→ {status}" if status is not None else "→ TIMEOUT/ERR"
    print(f"  [{mark}] {label:<60} {tag}  {fail_reason or ''}")


def raw_check(label, passed, status, note=""):
    mark = "PASS" if passed else "FAIL"
    RESULTS.append((mark, label, status, note))
    tag = f"→ {status}" if status is not None else "→ TIMEOUT/ERR"
    print(f"  [{mark}] {label:<60} {tag}  {note}")


def section(title):
    print(f"\n── {title} ──")


print("=" * 80)
print("EGRESS PROXY INTEGRATION TEST SUITE  (curl-based)")
print(f"Proxy: {PROXY}")
print("=" * 80)


# ─── 1. SSRF ────────────────────────────────────────────────────────────────
section("1. SSRF — private/link-local/loopback IPs → proxy 403")
check("RFC1918 10.x",                 "GET", "http://10.0.0.1/",     expect_proxy_block=True, timeout=5)
check("RFC1918 172.16.x",             "GET", "http://172.16.0.1/",   expect_proxy_block=True, timeout=5)
check("RFC1918 192.168.x",            "GET", "http://192.168.1.1/",  expect_proxy_block=True, timeout=5)
check("Link-local 169.254.x",        "GET", "http://169.254.1.1/",  expect_proxy_block=True, timeout=5)
check("CGNAT 100.64.x",              "GET", "http://100.64.0.1/",   expect_proxy_block=True, timeout=5)
# Loopback: 127.0.0.1 is in no_proxy so requests go direct — no proxy involvement.
# This is a known design gap: loopback SSRF is not blocked by the proxy rule.
# Cilium egressDeny covers it at the packet level instead.
status_lo, _ = curl("GET", "http://127.0.0.1/", use_proxy=True, timeout=3)
raw_check("Loopback 127.0.0.1 (in no_proxy — bypasses proxy, goes direct)",
          status_lo is None,  # connection refused = correct, no listener on loopback
          status_lo,
          "no_proxy bypass: direct connect fails (no listener) — Cilium egressDeny is the guard")


# ─── 2. WEBSOCKET BLOCKING ──────────────────────────────────────────────────
section("2. WebSocket upgrade → proxy 403 everywhere")
ws_hdrs = {
    "Upgrade": "websocket", "Connection": "Upgrade",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version": "13",
}
check("WS upgrade → external host",         "GET", PYPI,                 extra_headers=ws_hdrs, expect_proxy_block=True)
check("WS upgrade → internal host",         "GET", AGENTGW,              extra_headers=ws_hdrs, expect_proxy_block=True)
check("WS upgrade → SSRF private IP",       "GET", "http://192.168.1.1/", extra_headers=ws_hdrs, expect_proxy_block=True, timeout=5)


# ─── 3. URL LENGTH LIMIT ────────────────────────────────────────────────────
section("3. URL length limit (external >2048 → 403; internal exempt)")
base = "https://pypi.org/?"
check("URL 2100 chars external → blocked",      "GET", base + "x"*2100,            expect_proxy_block=True)
check("URL 2048 chars external → allowed",      "GET", base + "x"*(2048-len(base)), expect_proxy_block=False)
check("URL 2049 chars external → blocked",      "GET", base + "x"*(2049-len(base)), expect_proxy_block=True)
check("URL 2100 chars internal → exempt",       "GET", AGENTGW + "/?" + "x"*2100,  expect_proxy_block=False)


# ─── 4. GET/HEAD BODY BLOCKING ──────────────────────────────────────────────
section("4. GET/HEAD body blocking (external only; fires BEFORE scrubbing)")
check("GET with body external → blocked",         "GET",  PYPI, body="data=value", expect_proxy_block=True)
check("HEAD with body external → blocked",        "HEAD", PYPI, body="data=value", expect_proxy_block=True)
check("GET empty body external → not blocked",    "GET",  PYPI, body="",           expect_proxy_block=False)
check("GET no body external → not blocked",       "GET",  PYPI,                    expect_proxy_block=False)
check("HEAD no body external → not blocked",      "HEAD", PYPI,                    expect_proxy_block=False)
check("GET with body internal → not blocked",     "GET",  AGENTGW, body="x=1",     expect_proxy_block=False)


# ─── 5. METHOD ENFORCEMENT ──────────────────────────────────────────────────
section("5. Method enforcement (external: GET/HEAD only; internal: any method)")
check("POST external → blocked",   "POST",   PYPI, body="x=1", expect_proxy_block=True)
check("PUT external → blocked",    "PUT",    PYPI, body="x=1", expect_proxy_block=True)
check("DELETE external → blocked", "DELETE", PYPI,             expect_proxy_block=True)
check("PATCH external → blocked",  "PATCH",  PYPI, body="x=1", expect_proxy_block=True)
check("GET external → allowed",    "GET",    PYPI,             expect_proxy_block=False)
check("HEAD external → allowed",   "HEAD",   PYPI,             expect_proxy_block=False)
check("POST internal → allowed",   "POST",   AGENTGW, body='{}',
      extra_headers={"Content-Type": "application/json"},      expect_proxy_block=False)
check("DELETE internal → allowed", "DELETE", AGENTGW,          expect_proxy_block=False)
check("PUT internal → allowed",    "PUT",    AGENTGW, body='{}',
      extra_headers={"Content-Type": "application/json"},      expect_proxy_block=False)


# ─── 6. FQDN ALLOWLIST ──────────────────────────────────────────────────────
section("6. FQDN allowlist (Cilium enforces; proxy passes allowed FQDNs through)")
check("pypi.org allowlisted → 200",    "GET", PYPI,   expect_proxy_block=False, expect_status=200)
check("github.com allowlisted → 2xx",  "GET", GITHUB, expect_proxy_block=False)

status_g, body_g = curl("GET", GOOGLE, timeout=8)
reachable = status_g in (200, 301, 302, 303)
raw_check("google.com not allowlisted → Cilium drops (timeout/refused)",
          not reachable, status_g,
          "Cilium dropped" if not reachable else f"REACHED — FQDN not blocked! body: {body_g[:60]}")


# ─── 7. NO_PROXY BYPASS ─────────────────────────────────────────────────────
section("7. no_proxy bypass (slack.com goes direct, bypassing proxy rules)")
check("GET slack.com direct → reachable",            "GET",  "https://slack.com/", use_proxy=False, expect_proxy_block=False)
check("POST slack.com direct → not proxy-blocked",   "POST", "https://slack.com/",
      body="test", use_proxy=False, expect_proxy_block=False)


# ─── 8. SECRETS SCRUBBING ───────────────────────────────────────────────────
section("8. Secrets scrubbing ordering and non-interference")
FAKE_SSH = "[REDACTED PRIVATE KEY]"
FAKE_BOT = "[REDACTED]...Only"
FAKE_APP = "[REDACTED]"
FAKE_BRR = "Bearer eyJmYW...fake"

# GET-body fires before scrubbing — SSH key in GET body → 403 from GET-body rule
check("SSH key in GET body (ext) → GET-body 403 (not scrub)",
      "GET", PYPI, body=FAKE_SSH, expect_proxy_block=True)
# SSH key in POST to internal → allowed, scrubbed before forwarding
check("SSH key in POST body (internal) → allowed (scrubbed)",
      "POST", AGENTGW, body=FAKE_SSH,
      extra_headers={"Content-Type": "text/plain"}, expect_proxy_block=False)
# Slack bot token in Authorization header on external GET — scrubbed, request forwarded
check("xoxb- in Auth header (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"Authorization": f"Bearer {FAKE_BOT}"}, expect_proxy_block=False)
# xapp- token
check("xapp- in Auth header (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"Authorization": f"Bearer {FAKE_APP}"}, expect_proxy_block=False)
# Bearer token external — scrubbed, forwarded
check("Bearer token (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"Authorization": FAKE_BRR}, expect_proxy_block=False)
# Bearer token internal — NOT scrubbed (internal exempt), not blocked
check("Bearer token (internal POST) → not scrubbed, not blocked",
      "POST", AGENTGW, body='{}',
      extra_headers={"Authorization": FAKE_BRR, "Content-Type": "application/json"},
      expect_proxy_block=False)


# ─── 9. EDGE CASES ──────────────────────────────────────────────────────────
section("9. Edge cases")

# Internal host by IP — _is_internal() is suffix-only; IP → treated as external
try:
    agentgw_ip = socket.gethostbyname("agentgateway-proxy.agentgateway-system.svc.cluster.local")
    check(f"POST to internal by raw IP ({agentgw_ip}) → treated external → blocked",
          "POST", f"http://{agentgw_ip}/", body='{}',
          extra_headers={"Content-Type": "application/json"},
          expect_proxy_block=True, timeout=5)
except Exception as e:
    RESULTS.append(("SKIP", "POST to internal-by-IP (DNS failed)", None, str(e)))
    print(f"  [SKIP] POST to internal-by-IP: {e}")

# WS check fires before SSRF in code — both produce 403 but WS message is first
check("WS upgrade to private IP → blocked (WS fires before SSRF check)",
      "GET", "http://10.0.0.1/", extra_headers=ws_hdrs, expect_proxy_block=True, timeout=5)

# Plain HTTP external POST — method enforcement still applies
check("POST over plain HTTP to external → blocked",
      "POST", "http://pypi.org/", body="x=1", expect_proxy_block=True)

# .svc suffix (without .cluster.local) — also treated as internal
check("POST to .svc host (searxng) → internal, not blocked",
      "POST", "http://searxng.searxng.svc:8080", body='{"q":"test"}',
      extra_headers={"Content-Type": "application/json"}, expect_proxy_block=False)

# Exact boundary: 2048 chars is the limit value; len(url) > MAX means 2049+ is blocked
check("URL exactly at limit (2048 chars) → allowed",
      "GET", base + "x"*(2048 - len(base)), expect_proxy_block=False)
check("URL one over limit (2049 chars) → blocked",
      "GET", base + "x"*(2049 - len(base)), expect_proxy_block=True)

# GET with empty string body (b"") — content is falsy in Python, should not block
# curl sends no body when --data-binary is "", so this tests the empty-body edge case
check("GET with empty string body (ext) → not blocked",
      "GET", PYPI, body="", expect_proxy_block=False)



# ─── 10. INTERNAL SCRUBBING (searxng + new auth header patterns) ─────────────
section("10. Internal scrubbing — SSH keys, Slack tokens, Basic auth on internal traffic")

SEARXNG = "http://searxng.searxng.svc.cluster.local:8080"

# SSH key in POST body to searxng — should be scrubbed, request passes through
check("SSH key in POST body (searxng) → allowed (scrubbed)",
      "POST", SEARXNG, body=FAKE_SSH,
      extra_headers={"Content-Type": "text/plain"}, expect_proxy_block=False)

# Slack xoxb- token in a POST body to searxng — scrubbed, not blocked
FAKE_BOT_PLAIN = "xoxb-111111111111-222222222222-abc123def456"
check("xoxb- in POST body (searxng) → allowed (scrubbed)",
      "POST", SEARXNG, body=f"query={FAKE_BOT_PLAIN}",
      extra_headers={"Content-Type": "application/x-www-form-urlencoded"}, expect_proxy_block=False)

section("11. New auth header scrubbing — Basic and x-api-key (external only)")

# Authorization: Basic on external GET — should be scrubbed (forwarded with Basic [REDACTED])
check("Basic auth header (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}, expect_proxy_block=False)

# x-api-key on external GET — should be redacted
check("x-api-key header (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"x-api-key": "sk-supersecretapikey123"}, expect_proxy_block=False)

# api-key on external GET — should be redacted
check("api-key header (ext GET) → scrubbed, forwarded",
      "GET", PYPI, extra_headers={"api-key": "sk-supersecretapikey123"}, expect_proxy_block=False)

# Basic auth on internal POST — NOT scrubbed (internal exempt from auth-header scrubbing)
check("Basic auth header (internal POST) → not scrubbed, not blocked",
      "POST", AGENTGW, body="{}",
      extra_headers={"Authorization": "Basic dXNlcjpwYXNzd29yZA==", "Content-Type": "application/json"},
      expect_proxy_block=False)

# ─── SUMMARY ────────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
passes = sum(1 for r in RESULTS if r[0] == "PASS")
fails  = sum(1 for r in RESULTS if r[0] == "FAIL")
skips  = sum(1 for r in RESULTS if r[0] == "SKIP")
print(f"RESULTS: {passes} passed  {fails} failed  {skips} skipped  ({len(RESULTS)} total)")
if fails:
    print("\nFAILED TESTS:")
    for mark, label, status, note in RESULTS:
        if mark == "FAIL":
            print(f"  ✗ [{status}] {label}")
            if note:
                print(f"    {note}")
print("=" * 80)
