# Host MCP control plane

This directory contains the host-side MCP scaffold for vicegerent. It manages MCP servers that belong in the human's macOS GUI session: browser OAuth, keychain/cache state, kubeconfig, AWS SSO, VPN, and other laptop-local context.

The stack shape is:

```text
Hermes sandbox
  -> agentgateway
  -> ghostunnel (mTLS)
  -> Caddy filtered endpoint on the host (POST /mcp only)
  -> mcp-proxy-server :3663 (aggregate stdio MCP)
  -> stdio MCP servers (Notion, Linear, Kubernetes, ...)
```

`mcp-proxy-server` is the transport engine. Its admin UI is localhost-only/debug-only; the ghostunnel target is the filtered Caddy port, not the raw proxy port. Each infrastructure process (proxy, Caddy, ghostunnel) is managed by supervisord with independent auto-restart.

## Files

- `servers.json` — repo-owned registry of host MCP servers (what exists and its defaults)
- `vicegerent_mcp.py` — control helper: renders config, manages supervisord, hot-reload, OAuth state
- `requirements-host.txt` — Python dependencies (`rich`)
- `scripts/host/vicegerent-mcp` — executable wrapper

## Prerequisites

```bash
brew install caddy ghostunnel node op supervisord
pip install -r host/mcp/requirements-host.txt
# install k8s-mcp-server: https://gitlab.hahomelabs.com/kagent/k8s-mcp-server
```

Clone and build the proxy engine outside this repo:

```bash
git clone https://github.com/ptbsare/mcp-proxy-server.git ~/HomeLab/mcp-proxy-server
cd ~/HomeLab/mcp-proxy-server
npm ci
npm run build
```

## Start the host MCP stack

```bash
./scripts/host/vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
```

`start` renders runtime config, patches the proxy listener to bind `127.0.0.1`, applies the `notifications/tools/list_changed` patch so Hermes auto-refreshes on reload, generates a persisted admin password and session secret, then launches supervisord which manages proxy, Caddy, and ghostunnel with auto-restart.

The tunnel uses port `8453`. Override only when also updating the cluster-side backend:

```bash
./scripts/host/vicegerent-mcp start \
  --proxy-dir ~/HomeLab/mcp-proxy-server \
  --listen 192.168.64.1:8453
```

Check status (rich colored tables):

```bash
./scripts/host/vicegerent-mcp status
```

Stop everything:

```bash
./scripts/host/vicegerent-mcp stop
```

## Enable / disable servers

Runtime enable/disable without touching `servers.json` or restarting the stack:

```bash
./scripts/host/vicegerent-mcp disable notion
./scripts/host/vicegerent-mcp enable notion
```

State is stored in `~/.vicegerent/mcp/state.json` (not committed). When the stack is running, each command hot-reloads the proxy and sends `notifications/tools/list_changed` to all connected MCP clients. Hermes receives this via agentgateway and auto-refreshes its tool list — no `/reload-mcp` needed.

To list servers without the stack running:

```bash
./scripts/host/vicegerent-mcp list
```

To force re-render and reload after a `git pull` updates `servers.json`:

```bash
./scripts/host/vicegerent-mcp reload --proxy-dir ~/HomeLab/mcp-proxy-server
```

## Runtime state files

```text
~/.vicegerent/mcp/state.json              # enable/disable overrides (not committed)
~/.vicegerent/mcp/admin_password          # persisted proxy admin password (chmod 600)
~/.vicegerent/mcp/session_secret          # persisted proxy session secret (chmod 600)
~/.vicegerent/mcp/supervisord.conf        # generated supervisord config
~/.vicegerent/mcp/logs/                   # per-process logs
~/.vicegerent/mcp/mcp-proxy-server/config/ # rendered mcp-proxy-server config
~/.vicegerent/mcp/caddy/Caddyfile         # rendered Caddy config
```

## Test the filtered endpoint

The filtered endpoint listens on `127.0.0.1:3777` and permits only `POST /mcp`. Everything else is `404`.

Negative checks:

```bash
curl -i http://127.0.0.1:3777/admin
curl -i http://127.0.0.1:3777/admin/terminal
curl -i http://127.0.0.1:3777/sse
curl -i http://127.0.0.1:3777/mcp        # GET — should 404
```

Positive checks use MCP StreamableHTTP `POST /mcp` against `127.0.0.1:3777`.

## Kubernetes

The `kubernetes` server uses `~/.kube/config` and exposes every kubeconfig context via a required `context` tool argument:

```json
{"context": "uw1-prod1", "kind": "Pod", "namespace": "default"}
```

## Auth state

`mcp-remote` stores OAuth state in `~/.mcp-auth/mcp-remote-<version>/`. The helper tracks state per server:

```bash
./scripts/host/vicegerent-mcp auth-status notion
./scripts/host/vicegerent-mcp auth-status linear
./scripts/host/vicegerent-mcp doctor
```

States: `authenticated` · `auth-in-progress` · `auth-incomplete` · `auth-needed` · `unknown`

## Auth reset

Stop the stack before deleting OAuth cache to avoid wedged PKCE flows:

```bash
./scripts/host/vicegerent-mcp stop
./scripts/host/vicegerent-mcp auth-reset notion --yes
./scripts/host/vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
```

`auth-reset` refuses to delete cache files if matching MCP processes are alive unless `--force` is supplied.

## mcp-proxy-server patch notes

`start` applies two patches to the mcp-proxy-server source and rebuilds if needed:

1. **Loopback bind** — binds the listener to `127.0.0.1` instead of all interfaces so the raw admin UI is never exposed.
2. **list_changed notification** — after `POST /admin/server/reload`, sends `notifications/tools/list_changed` to all connected SSE and StreamableHTTP sessions so Hermes auto-refreshes its tool list.

Both patches are idempotent. After updating mcp-proxy-server with `git pull && npm run build`, run `vicegerent-mcp start` again to re-apply.
