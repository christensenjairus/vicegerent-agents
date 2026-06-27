# Host MCP control plane

This directory contains the host-side MCP scaffold for vicegerent. It is for MCPs that belong in the human's macOS GUI session: browser OAuth, keychain/cache state, kubeconfig, AWS SSO, VPN, and other laptop-local context.

The validated v1 shape is:

```text
Hermes sandbox
  -> agentgateway
  -> ghostunnel
  -> Caddy filtered endpoint on the host
  -> mcp-proxy-server /mcp
  -> stdio mcp-remote
  -> hosted Notion MCP
```

`mcp-proxy-server` is treated as the transport engine, not the user-facing product UI. Its admin UI is localhost-only/debug-only. The ghostunnel target must be the filtered Caddy port, not the raw proxy port, because the proxy admin UI includes a host terminal.

## Files

- `servers.json` - repo-owned host MCP registry. It currently includes Notion, Linear, and Jira/Atlassian Rovo remote-OAuth entries.
- `vicegerent_mcp.py` - no-dependency helper that renders runtime config and manages auth cache state.
- `scripts/host/vicegerent-mcp` - executable wrapper.

## Prerequisites

```bash
brew install caddy ghostunnel node op
```

Clone and build the proxy engine outside this repo:

```bash
git clone https://github.com/ptbsare/mcp-proxy-server.git ~/HomeLab/mcp-proxy-server
cd ~/HomeLab/mcp-proxy-server
npm ci
npm run build
```

## Render runtime config

From this repo:

```bash
./scripts/host/vicegerent-mcp render
```

This writes runtime files under `~/.vicegerent/mcp`:

```text
~/.vicegerent/mcp/mcp-proxy-server/config/mcp_server.json
~/.vicegerent/mcp/mcp-proxy-server/config/tool_config.json
~/.vicegerent/mcp/caddy/Caddyfile
~/.vicegerent/mcp/proxy.env
```

Copy the rendered proxy config into the proxy checkout:

```bash
mkdir -p ~/HomeLab/mcp-proxy-server/config
cp -R ~/.vicegerent/mcp/mcp-proxy-server/config/. ~/HomeLab/mcp-proxy-server/config/
```

## Start the host MCP stack

The helper starts all three host processes when at least one MCP server is enabled:

```text
mcp-proxy-server :3663  (raw admin + local aggregate MCP)
Caddy            :3777  (filtered POST /mcp only)
ghostunnel       :8453  (mTLS tunnel to Caddy :3777)
```

Run:

```bash
./scripts/host/vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
```

The helper renders runtime config, copies proxy config into the proxy checkout, patches and verifies the proxy listener binds `127.0.0.1` instead of all interfaces, generates a local admin password under `~/.vicegerent/mcp/admin_password`, starts Caddy with the HTTP-only filter, and starts `scripts/ghostunnel/ghostshell.sh` with `TARGET=127.0.0.1:3777` and `LISTEN=$HOST_ONLY_IP:8453`.

The host MCP tunnel intentionally uses `8453`, not `8443`; `8443` is the default tunnel for the existing host-side Kubernetes MCP. Override only when you also update the cluster-side backend:

```bash
./scripts/host/vicegerent-mcp start \
  --proxy-dir ~/HomeLab/mcp-proxy-server \
  --listen 192.168.64.1:8453
```

This MR is the host-side control plane. Cluster-side `apps/vicegerent/mcps/*` backend/route wiring for Notion, Linear, and Jira is a follow-up before agents can call these tools through agentgateway.

Check status:

```bash
./scripts/host/vicegerent-mcp status
```

Stop everything it started:

```bash
./scripts/host/vicegerent-mcp stop
```

The raw proxy listens on `127.0.0.1:3663`. Use its admin UI only from the host:

```text
http://127.0.0.1:3663/admin
```

For OAuth-backed stdio backends, `proxy.env` disables stdio tool-call retries. Retrying at the proxy layer causes repeated browser OAuth attempts and can wedge PKCE flows.

## Test the filtered endpoint

The filtered endpoint listens on `127.0.0.1:3777` and permits only `POST /mcp`. Everything else is `404`.

Negative checks:

```bash
curl -i http://127.0.0.1:3777/admin
curl -i http://127.0.0.1:3777/admin/terminal
curl -i http://127.0.0.1:3777/sse
curl -i http://127.0.0.1:3777/message
curl -i http://127.0.0.1:3777/mcp
```

All should return `404`. The last one is a GET and should not be forwarded.

Positive checks should use MCP StreamableHTTP `POST /mcp` against `127.0.0.1:3777`. By default the helper leaves the proxy MCP endpoint without `ALLOWED_KEYS`; host access is gated by ghostunnel mTLS plus the Caddy path filter.

## Auth state

Notion is configured via:

```text
npx -y mcp-remote https://mcp.notion.com/mcp
```

`mcp-remote` stores OAuth state in `~/.mcp-auth/mcp-remote-<version>/`. The helper computes the same server URL hash as `mcp-remote` and reports targeted state:

```bash
./scripts/host/vicegerent-mcp auth-status notion
./scripts/host/vicegerent-mcp auth-status linear
./scripts/host/vicegerent-mcp auth-status jira
./scripts/host/vicegerent-mcp doctor
```

States:

- `authenticated` - `tokens.json` has access and refresh tokens.
- `auth-in-progress` - a live lock file is coordinating browser OAuth.
- `auth-incomplete` - client/verifier files exist but tokens are missing.
- `auth-needed` - token file exists but is not usable.
- `unknown` - no cache files found.

## Auth reset

Do not delete `~/.mcp-auth` while `mcp-remote` or `mcp-proxy-server` is running. That leaves tools listed in memory but real calls can enter browser/Unauthorized redirect loops.

The safe reset sequence is:

```bash
# stop the stack first
./scripts/host/vicegerent-mcp stop
./scripts/host/vicegerent-mcp auth-reset notion --yes
# start again and complete browser OAuth
./scripts/host/vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
./scripts/host/vicegerent-mcp auth-status notion
```

`auth-reset` refuses to delete cache files if matching MCP processes appear to be alive unless `--force` is supplied.

## Validated behavior

- Notion OAuth opens a browser through `mcp-remote`.
- Notion tools are visible through the local proxy's StreamableHTTP `/mcp` endpoint.
- Toggling `active` in the proxy config and calling `/admin/server/reload` makes tools disappear/reappear.
- Restarting the proxy reuses `mcp-remote` tokens without opening a browser.
- Corrupting only `access_token` causes `mcp-remote` to refresh via `refresh_token` and repair `tokens.json`.
- Corrupting both access and refresh tokens enters a browser/Unauthorized loop; treat that as `auth-needed` and perform the safe reset sequence.
