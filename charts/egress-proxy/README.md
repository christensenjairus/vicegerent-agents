# Egress Proxy — Security Model

The egress proxy is an mitmproxy instance that sits between the **hermes agent sandbox** and all outbound HTTP(S) traffic. It provides scrubbing, method enforcement, and an audit log. It is **not** a complete security boundary on its own — it works alongside Cilium network policy and the Sandbox CRD's inherent isolation.

> **Scope**: this proxy guards only sandbox containers (hermes). Platform services — searxng, tavily, firecrawl, agentgateway itself — are not routed through it. Those services have their own network policies and are not in scope for this scrubbing layer.

---

## What the proxy enforces

### Secrets scrubbing
Applied to every request — headers and body — before forwarding to any destination, internal (agentgateway, searxng) or external (internet). **Two layers** run on each scrubbed string: a hand-rolled regex registry (`REDACT_PATTERNS`) then gitleaks' ~180-rule default ruleset via the in-Pod `gitleaks-sidecar` container over loopback (`images/egress-gitleaks-sidecar`). The registry mirrors `mcp-cerbos-shim`'s `secretPatternRegistry` — keep the two in sync by hand.

| Pattern | What it catches |
|---|---|
| `-----BEGIN ... PRIVATE KEY-----` | SSH private keys — RSA, EC, Ed25519, OpenSSH, PKCS#8 encrypted |
| `xox[bpraescd]-...` / `xapp-...` | Slack tokens — bot, app-level, user, refresh, socket, client, app-config |
| `Bearer <token>` / `Basic <creds>` | auth values in URL/body; the `Authorization` header itself is stripped on every request |
| `x-api-key` / `api-key` headers | API-key headers redacted on every request |
| AWS / GitHub / GitLab / Google | `AKIA…`, `gh?_…`, `glpat-…`, `AIza…` |
| OpenAI / Anthropic / Stripe | `sk-…` / `sk-proj-…`, `sk-ant-…`, `sk_live_…` / `rk_test_…` |
| Notion / Twilio / npm / JWT | `ntn_…`, `SK<32 hex>`, `npm_…`, `eyJ….eyJ….…` |
| gitleaks default ruleset | ~180 provider tokens/keys/connection strings the registry doesn't name (SendGrid, etc.) |

The request **URL path and query** are also scrubbed on every request, and response bodies are scrubbed (non-streaming only) to guard against echo attacks — both through the same two-layer `_redact()`.

### Method enforcement
GET and HEAD only for external destinations. POST, PUT, PATCH, DELETE → 403. Internal cluster services (agentgateway, searxng) may use any method — they require POST and hold no sandbox secrets. Exception: `git-upload-pack` (smart-HTTP clone/fetch, read-only) is allowed through so `pre-commit` can install hook repos.

### URL length limit
External URLs over 2048 characters → 403. Blocks naive base64/hex query-string exfiltration. All legitimate API and search URLs are well under this limit.

### GET/HEAD body blocking
GET and HEAD requests with a non-empty body to external destinations → 403. GET bodies have no legitimate use case here and are a potential exfiltration channel.

### WebSocket blocking
`Upgrade: websocket` headers → 403 in the `request()` hook. `websocket_start` hook kills any connection that slips through. Applies everywhere.

### SSRF protection
Requests to RFC1918, link-local (169.254/16), loopback, and CGNAT (100.64/10) ranges → 403. Defence-in-depth alongside Cilium's `egressDeny` rules.

### Audit log
Every request emits a JSON log line (one object per line, `message` carries the same content the pre-JSON format used):
```
{"time": "2026-07-08T22:14:03+0000", "level": "INFO", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 ALLOW internal=False method=GET url=https://pypi.org/simple/requests/"}
{"time": "2026-07-08T22:14:03+0000", "level": "WARNING", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 BLOCKED method=POST url=https://api.github.com/repos/..."}
```
View with: `kubectl logs -n egress-proxy deploy/egress-proxy` — pipe through `jq` for readability, e.g. `... | jq -r .message`.

---

## What the proxy does NOT enforce

### Destination content policy
The proxy checks the HTTP *method*, not the *URL path* or *response content*. A GET to any allowed FQDN succeeds regardless of path. This is intentional — path-based policy requires constant maintenance and breaks legitimate use cases.

**Mitigation**: The Cilium FQDN allowlist (rendered into `charts/egress-proxy/templates/networkpolicy.yaml`) is the destination gate. Only explicitly listed FQDNs are reachable. Add FQDNs in `cluster-vars.yaml`, not URL path rules — see [Adding a new external service](#adding-a-new-external-service).

### Sophisticated GET exfiltration
A URL within the 2048-char limit can still carry meaningful data in query strings or path segments. Encoding (base64, hex, split-chunking) bypasses pattern scrubbing. This is a fundamental limitation of HTTP-layer inspection.

**Accepted risk**: The FQDN allowlist limits the set of reachable destinations. Exfiltration requires a reachable destination that accepts and stores GET parameters — an attacker needs prior access to configure such an endpoint.

### Secrets not caught by either layer
The regex registry covers the named provider shapes in the table above; gitleaks adds its ~180-rule default set on top. A credential in neither — a bespoke internal token format, or any secret carried **encoded** (base64, hex, split-chunked) — still passes through. Pattern/rule matching is raw-value only.

**To add a regex pattern**: edit `REDACT_PATTERNS` in `charts/egress-proxy/templates/addon-configmap.yaml` (and mirror it into `mcp-cerbos-shim`'s `secretPatternRegistry` if MCP tool calls should also catch it). gitleaks rules are upstream — bump the sidecar's pinned gitleaks version to pick up new ones. For verbatim secret values, see below.

**To scrub a literal secret value**: there is currently no mechanism to inject runtime secret values into the proxy for scrubbing. Adding this requires mounting the secret into the proxy pod and loading it at startup — a future improvement.

### SSH traffic
Port 22 egress to `github.com` and `gitlab.hahomelabs.com` is direct — bypasses the proxy entirely. `git push` content is not inspectable at the HTTP layer. The SSH deploy key's scope (read-only vs read-write, per-repo vs org-wide) is the control here.

### Slack traffic
Four specific Slack FQDNs are allowed direct (bypassing the proxy) via the **hermes** Cilium policy (`charts/agent/templates/networkpolicy.yaml`, FQDNs set through `networkAllowlist.slackFQDNs` in `apps/personal/agents/hermes/values.yaml`) and `no_proxy` in `sandbox.yaml`. Slack Socket Mode requires POST and WebSocket — both blocked by the proxy — so Slack must go direct. (`no_proxy` alone is not enough: `slack_sdk` ignores `NO_PROXY` and auto-loads `HTTPS_PROXY`, so the hermes image also carries build patch `0007-slack-bypass-egress-proxy.py` to force the bypass.)

| FQDN | Purpose |
|---|---|
| `slack.com` | Web API (`slack.com/api/*`) — all bot API calls |
| `wss-primary.slack.com` | Socket Mode WebSocket (primary endpoint) |
| `wss-backup.slack.com` | Socket Mode WebSocket (failover endpoint) |
| `files.slack.com` | File/image downloads for attachment handling |

The former `*.slack.com` wildcard is removed. If Slack rotates the WSS hostname, Socket Mode reconnections will fail — add the new hostname to `networkAllowlist.slackFQDNs` in `apps/personal/agents/hermes/values.yaml` and `no_proxy` in `sandbox.yaml`. Slack traffic carries no sandbox secrets by design.

### Streaming responses
SSE (`text/event-stream`) and chunked transfer responses skip response body scrubbing to avoid buffering the LLM stream. An echo attack via streaming is theoretically possible but requires the external server to actively reflect back injected content.

---

## Bypass vectors

### Cannot bypass (Cilium enforces at kernel level)
- Direct TCP to internet FQDNs not in the allowlist — Cilium drops the packet
- Direct TCP to internet IPs (bypassing proxy) — Cilium allows only proxy:8080 from sandbox
- Non-HTTP protocols on port 443 — Cilium allows the port but mitmproxy rejects non-HTTP

### Difficult to exploit in practice
- **GET query string exfiltration** — URL length limit constrains payload size; destination must be in FQDN allowlist and must store/forward the data
- **Encoded secrets** — scrubbing patterns match raw values; base64/hex encoding evades them, but encoding is a deliberate extra step requiring tool access

### Residual risks
- **DNS rebinding** — `_is_private()` in the addon only checks literal IP addresses; hostname-based requests bypass it (intentional — Cilium enforces the destination boundary). Cilium FQDN policy caches DNS results with a TTL; within that window a rebinding attack (attacker-controlled FQDN, TTL 0, rebind to RFC1918) could reach a private IP via an allowed FQDN. The `egressDeny` CiliumNetworkPolicy rules are the actual guard here — they operate at the packet level and block the private-IP egress regardless of what DNS returned. Short exploitation window; requires attacker-controlled DNS infrastructure.
- **`no_proxy` override in subprocesses** — a subprocess could set `NO_PROXY=*`, causing it to attempt direct egress which Cilium then drops. Fails noisily rather than silently exfiltrating.
- **IPv6** — the hermes pod has no IPv6-specific FQDN allowlist. However, the `egressDeny` CiliumNetworkPolicy includes `::1/128`, `fc00::/7`, and `fe80::/10` to block private IPv6 ranges. Direct IPv6 internet egress from tools that ignore `http_proxy` would need a non-private IPv6 destination; the Cilium default deny covers the rest.

---

## Adding a new external service

There are two CiliumNetworkPolicies; pick by how the sandbox reaches the service.

For a service the **proxy fetches** (GET/HEAD through the egress proxy):
1. Add the FQDN to `clusters/<machine>/cluster-vars.yaml` — one edit, single source of truth. `apexWildcardDomains` if the service also needs subdomains (an exact match plus a `*.<domain>` wildcard); `exactOnlyDomains` for an exact host only. Both are comma-joined bare hostnames, machine-scoped (same laptop implies the same network requirements, unlike the per-agent direct-egress bypass FQDNs below). Flux substitutes them into `charts/egress-proxy`, which renders the **same** list into both the Cilium `toFQDNs` policy (the kernel-level gate) and `scrub.py`'s allowlist (the mitmproxy application-layer gate) — so the two can no longer drift.
2. If the service needs POST it cannot go through the proxy (external POST → 403) — route it direct instead (below)
3. If the service holds credentials, add a `REDACT_PATTERNS` entry for its token format For a service the sandbox reaches **direct** (bypassing the proxy, e.g. Slack):
1. Add the FQDN to `networkAllowlist.slackFQDNs` in `apps/personal/agents/hermes/values.yaml` (rendered by `charts/agent/templates/networkpolicy.yaml`)
2. Add it to `no_proxy`/`NO_PROXY` in `sandbox.yaml`

---

## Adding a new secret pattern

Append a compiled regex to `REDACT_PATTERNS` in `charts/egress-proxy/templates/addon-configmap.yaml`, e.g.:

```python
re.compile(r"mycorp_tok_[A-Za-z0-9]{32}", re.ASCII),
```

Add a matching fixture to `scripts/test-scrub-patterns.py`, and mirror the pattern into `mcp-cerbos-shim`'s `secretPatternRegistry` if MCP tool-call payloads should catch it too. Reloader restarts the proxy pod automatically when the ConfigMap changes. (Provider tokens already in gitleaks' default ruleset need no registry entry — they are caught by the sidecar layer.)
