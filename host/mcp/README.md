# Host MCP control plane

This directory manages MCP servers that belong in the human's macOS GUI session: browser OAuth, kubeconfig, AWS SSO, and other laptop-local context.

Stack shape:

```text
Hermes sandbox
  -> agentgateway
  -> ghostunnel (mTLS, port 8453)
  -> Caddy filtered endpoint (POST /mcp only, port 3777)
  -> mcp-proxy-server (aggregate stdio MCP, port 3663)
  -> stdio MCP servers (Notion, Linear, Kubernetes ...)
```

Each infrastructure process (proxy, Caddy, ghostunnel) runs under supervisord with `autorestart=true` — a crash in one does not take down the others.

## Files

- `servers.json` — repo-owned registry (what servers exist and their defaults)
- `vicegerent_mcp.py` — control helper: supervisord, hot-reload, OAuth state
- `requirements-host.txt` — Python dependencies (`rich`)
- `scripts/host/vicegerent-mcp` — thin bash wrapper

## Prerequisites

```bash
brew install caddy ghostunnel node op supervisor
pip install -r host/mcp/requirements-host.txt
# build the in-repo Kubernetes MCP server (requires Go):
make -C host/k8s-mcp-server
```

Clone and build mcp-proxy-server outside this repo:

```bash
git clone https://github.com/ptbsare/mcp-proxy-server.git ~/HomeLab/mcp-proxy-server
cd ~/HomeLab/mcp-proxy-server && npm ci && npm run build
```

## Subcommands

```
list          show all configured servers and their state (no stack required)
status        show server auth state + infrastructure process state (rich tables)
enable KEY    enable a server and hot-reload the proxy
disable KEY   disable a server and hot-reload the proxy
reload        re-render proxy config and hot-reload (use after git pull)
start         start proxy, Caddy, and ghostunnel via supervisord
stop          shut down supervisord and all managed processes
logs PROCESS  tail logs for proxy|caddy|ghostunnel|supervisord (Ctrl-C to exit)
auth-status   show mcp-remote OAuth cache state
auth-reset    delete OAuth cache for a server (stop stack first)
doctor        check host prerequisites and auth state
```

## Start the stack

```bash
./scripts/host/vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
```

`start` applies two idempotent patches to mcp-proxy-server, renders runtime config, and launches supervisord. After updating mcp-proxy-server (`git pull && npm run build`), run `start` again to re-apply patches.

## Enable / disable servers

Runtime enable/disable — does not touch `servers.json`:

```bash
./scripts/host/vicegerent-mcp disable notion
./scripts/host/vicegerent-mcp enable notion
```

State lives in `~/.vicegerent/mcp/state.json` (not committed). When the stack is running, each command hot-reloads the proxy and triggers `notifications/tools/list_changed` so Hermes auto-refreshes its tool list — no `/reload-mcp` needed.

After `git pull` adds new servers to `servers.json`:

```bash
./scripts/host/vicegerent-mcp reload --proxy-dir ~/HomeLab/mcp-proxy-server
```

## Status and logs

```bash
./scripts/host/vicegerent-mcp status          # rich tables
./scripts/host/vicegerent-mcp list            # no stack required
./scripts/host/vicegerent-mcp logs proxy      # tail proxy log (Ctrl-C to exit)
./scripts/host/vicegerent-mcp logs caddy
./scripts/host/vicegerent-mcp logs ghostunnel
./scripts/host/vicegerent-mcp logs supervisord
./scripts/host/vicegerent-mcp logs proxy -n 100  # show last 100 lines then follow
```

## Runtime state files

```text
~/.vicegerent/mcp/state.json              # runtime enable/disable overrides
~/.vicegerent/mcp/admin_password          # proxy admin password (chmod 600)
~/.vicegerent/mcp/session_secret          # proxy session secret (chmod 600)
~/.vicegerent/mcp/supervisord.conf        # generated supervisord config
~/.vicegerent/mcp/supervisor.sock         # supervisord control socket
~/.vicegerent/mcp/logs/                   # per-process logs
~/.vicegerent/mcp/mcp-proxy-server/config/ # rendered proxy config
~/.vicegerent/mcp/caddy/Caddyfile         # rendered Caddy config
```

## Test the filtered endpoint

Only `POST /mcp` is forwarded. Everything else returns `404`.

```bash
# All should 404:
curl -i http://127.0.0.1:3777/admin
curl -i http://127.0.0.1:3777/sse
curl -i http://127.0.0.1:3777/mcp          # GET — 404
```

## Kubernetes

The `kubernetes` server uses `~/.kube/config` and exposes every kubeconfig context via a `context` tool argument:

```json
{"context": "uw1-prod1", "kind": "Pod", "namespace": "default"}
```

The binary is built from source in this repo: `make -C host/k8s-mcp-server`.

## Auth state

`mcp-remote` stores OAuth tokens in `~/.mcp-auth/mcp-remote-<version>/`. Check state:

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

## mcp-proxy-server patches (applied at start)

1. **Loopback bind** — binds to `127.0.0.1` instead of all interfaces so the admin terminal UI is never reachable remotely.
2. **list_changed notification** — after `POST /admin/server/reload`, sends `notifications/tools/list_changed` to all connected SSE and StreamableHTTP sessions so Hermes auto-refreshes.

Both patches are idempotent and re-applied on `start` if a fresh `npm run build` overwrote them.
