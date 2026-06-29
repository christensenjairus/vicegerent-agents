# Egress Proxy — Security Model

The egress proxy is an mitmproxy instance that sits between the hermes agent sandbox
and all outbound HTTP(S) traffic. It provides scrubbing, method enforcement, and an
audit log. It is **not** a complete security boundary on its own — it works alongside
Cilium network policy and the Sandbox CRD's inherent isolation.

---

## What the proxy enforces

### Secrets scrubbing
Applied to every request — headers and body — before forwarding to any destination,
internal (agentgateway, searxng) or external (internet).

| Pattern | What it catches |
|---|---|
| `-----BEGIN ... PRIVATE KEY-----` | SSH private keys — RSA, EC, Ed25519, OpenSSH, PKCS#8 encrypted |
| `xox[bpraes]-[A-Za-z0-9\-_]+` | Slack tokens — bot, app-level, user, refresh, socket, client |
| `Authorization: Bearer <token>` | Bearer tokens stripped from headers on external requests |

Response bodies are also scrubbed (non-streaming only) to guard against echo attacks.

### Method enforcement
GET and HEAD only for external destinations. POST, PUT, PATCH, DELETE → 403.
Internal cluster services (agentgateway, searxng) may use any method — they require
POST and hold no sandbox secrets.

### URL length limit
External URLs over 2048 characters → 403. Blocks naive base64/hex query-string
exfiltration. All legitimate API and search URLs are well under this limit.

### GET/HEAD body blocking
GET and HEAD requests with a non-empty body to external destinations → 403.
GET bodies have no legitimate use case here and are a potential exfiltration channel.

### WebSocket blocking
`Upgrade: websocket` headers → 403 in the `request()` hook. `websocket_start` hook
kills any connection that slips through. Applies everywhere.

### SSRF protection
Requests to RFC1918, link-local (169.254/16), loopback, and CGNAT (100.64/10) ranges
→ 403. Defence-in-depth alongside Cilium's `egressDeny` rules.

### Audit log
Every request emits a structured log line:
```
ALLOW internal=False method=GET url=https://pypi.org/simple/requests/
RESPONSE method=GET status=200 url=https://pypi.org/simple/requests/
BLOCKED method=POST url=https://api.github.com/repos/...
REDACTED count=1 method=GET url=https://example.com/
```
View with: `kubectl logs -n egress-proxy deploy/egress-proxy`

---

## What the proxy does NOT enforce

### Destination content policy
The proxy checks the HTTP *method*, not the *URL path* or *response content*. A GET
to any allowed FQDN succeeds regardless of path. This is intentional — path-based
policy requires constant maintenance and breaks legitimate use cases.

**Mitigation**: The Cilium FQDN allowlist (`networkpolicy.yaml`) is the destination
gate. Only explicitly listed FQDNs are reachable. Add FQDNs there, not URL path rules.

### Sophisticated GET exfiltration
A URL within the 2048-char limit can still carry meaningful data in query strings or
path segments. Encoding (base64, hex, split-chunking) bypasses pattern scrubbing.
This is a fundamental limitation of HTTP-layer inspection.

**Accepted risk**: The FQDN allowlist limits the set of reachable destinations.
Exfiltration requires a reachable destination that accepts and stores GET parameters
— an attacker needs prior access to configure such an endpoint.

### Secrets not in REDACT_PATTERNS
Only SSH private keys and Slack tokens are scrubbed. Anthropic/OpenAI API keys and
other credentials are NOT currently scrubbed. If hermes emits these in a request body
or query string, they pass through.

**To add a pattern**: edit `REDACT_PATTERNS` in `addon-configmap.yaml`. Regex patterns
only. For verbatim secret values, see below.

**To scrub a literal secret value**: there is currently no mechanism to inject runtime
secret values into the proxy for scrubbing. Adding this requires mounting the secret
into the proxy pod and loading it at startup — a future improvement.

### SSH traffic
Port 22 egress to `github.com` and `gitlab.hahomelabs.com` is direct — bypasses the
proxy entirely. `git push` content is not inspectable at the HTTP layer. The SSH
deploy key's scope (read-only vs read-write, per-repo vs org-wide) is the control here.

### Slack traffic
`*.slack.com:443` is direct. Slack Socket Mode requires POST and WebSocket — both are
blocked by the proxy's rules, so Slack must bypass it. Slack traffic carries no
sandbox secrets (API keys, SSH keys) by design.

### Streaming responses
SSE (`text/event-stream`) and chunked transfer responses skip response body scrubbing
to avoid buffering the LLM stream. An echo attack via streaming is theoretically
possible but requires the external server to actively reflect back injected content.

---

## Bypass vectors

### Cannot bypass (Cilium enforces at kernel level)
- Direct TCP to internet FQDNs not in the allowlist — Cilium drops the packet
- Direct TCP to internet IPs (bypassing proxy) — Cilium allows only proxy:8080 from sandbox
- Non-HTTP protocols on port 443 — Cilium allows the port but mitmproxy rejects non-HTTP

### Difficult to exploit in practice
- **GET query string exfiltration** — URL length limit constrains payload size;
  destination must be in FQDN allowlist and must store/forward the data
- **Encoded secrets** — scrubbing patterns match raw values; base64/hex encoding evades
  them, but encoding is a deliberate extra step requiring tool access

### Residual risks
- **DNS rebinding** — Cilium FQDN policy caches DNS results with a TTL; within that
  window a rebinding attack could reach a private IP via an allowed FQDN. Short window,
  requires attacker-controlled DNS.
- **`no_proxy` override in subprocesses** — a subprocess could set `NO_PROXY=*`,
  causing it to attempt direct egress which Cilium then drops. Fails noisily rather
  than silently exfiltrating.
- **IPv6** — no explicit IPv6 FQDN policy on the hermes pod. If the node has IPv6
  internet connectivity, direct IPv6 egress from the sandbox may be possible for tools
  that ignore `http_proxy`. Mitigation: disable IPv6 on the minikube node if not needed.

---

## Adding a new external service

1. Add the FQDN to `networkpolicy.yaml` (egress-proxy policy, not the hermes policy)
2. If the service requires POST, note it as an accepted exception in this document
3. If the service holds credentials, add a `REDACT_PATTERNS` entry for its token format

---

## Adding a new secret pattern

Edit `REDACT_PATTERNS` in `addon-configmap.yaml`:

```python
# Example: Anthropic API keys
re.compile(r"sk-ant-[A-Za-z0-9\-_]{40,}", re.ASCII),
# Example: OpenAI API keys
re.compile(r"sk-[A-Za-z0-9]{48}", re.ASCII),
```

Reloader will restart the proxy pod automatically when the ConfigMap changes.
