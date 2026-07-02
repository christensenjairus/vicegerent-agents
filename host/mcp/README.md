# Host stack control plane

This directory owns the host-side ToolHive stack that backs the cluster's MCP
access. `vicegerent-mcp` brings up ToolHive workloads, aggregates them behind a
single vMCP endpoint, and exposes that to the cluster over mTLS.

Stack shape:

```text
Hermes sandbox
  -> agentgateway
  -> ghostunnel (mTLS, listen 127.0.0.1:8453, reached via host.docker.internal:8453)
  -> ToolHive vMCP (loopback 127.0.0.1:4483, prefixes each backend's tools with {workload}_)
  -> 6 ToolHive workloads (group 'vicegerent')
```

`thv` runs the workloads as Docker containers under ToolHive's own daemon —
they persist across `start`/`stop` so OAuth tokens are not re-prompted.
Supervisord manages only the three long-lived host processes:

- `caffeinate` — keeps macOS awake for as long as the stack is up.
- `vmcp` — `thv vmcp serve` aggregating the group on 127.0.0.1:4483.
- `ghostunnel` — terminates mTLS from the cluster (client CN `agent-client`)
  and forwards to vMCP.

## The 6 backends (group `vicegerent`)

The workload name is the vMCP tool prefix, and the Cerbos policy keys off it —
these names are load-bearing, defined in `toolhive-servers.json`:

| Workload | Run | Auth |
|---|---|---|
| `kubernetes` | npx `kubernetes-mcp-server` (`--read-only`) | kind `--internal` kubeconfig |
| `gitlab` | npx `@zereight/mcp-gitlab` | `gitlab_token` secret |
| `tavily` | npx `tavily-mcp` | `tavily_api_key` secret |
| `firecrawl` | npx `firecrawl-mcp` | `firecrawl_api_key` secret |
| `notion` | registry remote `notion-remote` | OAuth (browser, first run) |
| `linear` | registry remote `linear` | OAuth (browser, first run) |

Authorization (which tools an agent may call) lives in the cluster
(agentgateway allowlist + Cerbos), so the generated vMCP config exposes ALL
backend tools with NO optimizer, per-workload filter, or Cedar/authz.

### Kubernetes networking (the gotcha)

`thv` containerizes the npx k8s server, so it lives in the Docker VM and cannot
reach a host-loopback API. The container is attached to the kind docker network
(`--network vicegerent --isolate-network=false`) and fed kind's `--internal`
kubeconfig (`kind get kubeconfig --name vicegerent --internal`, server
`https://vicegerent-control-plane:6443`), mounted read-only and pointed at via
`KUBECONFIG` + `--kubeconfig`.

## Prerequisites

```bash
./vicegerent mcp setup      # brew: thv, ghostunnel, supervisor + Python venv
```

Then configure ToolHive secrets (once):

```bash
thv secret setup                    # choose 'encrypted' (persists OAuth tokens too)
thv secret set gitlab_token         # GitLab PAT (api scope)
thv secret set tavily_api_key
thv secret set firecrawl_api_key
```

`./vicegerent secrets setup platform` writes the host ghostunnel mTLS material
to `~/.vicegerent/ghostunnel`.

## Subcommands

```
start         bring up workloads + vMCP + ghostunnel (idempotent)
stop          shut down the supervised stack (workloads left running; --workloads to stop them too)
status        workload + supervised-process state (rich table)
logs PROC     tail logs for ghostunnel|vmcp|supervisord|caffeinate (Ctrl-C to exit)
doctor        check binaries, thv secrets provider + secrets, kind cluster
tui           interactive dashboard (textual)
```

```bash
./vicegerent-mcp start
./vicegerent-mcp status
./vicegerent-mcp tui
./vicegerent-mcp stop
```

For the full machine lifecycle use the top-level wrapper: `./vicegerent start`
resumes the Kind cluster then starts this stack; `./vicegerent stop` reverses it.

## Config + env

`toolhive-servers.json` declares the group, the vMCP port, and the 6 servers
(name, run type, package/registry, run flags, env, and thv secret mappings).
Overridable env:

```text
THV_GROUP               ToolHive group name (default: vicegerent)
VMCP_HOST / VMCP_PORT   vMCP loopback target (default 127.0.0.1:4483)
LISTEN                  ghostunnel listen address (default 127.0.0.1:8453)
```

## Runtime state files

```text
~/.vicegerent/mcp/supervisord.conf        # generated supervisord config
~/.vicegerent/mcp/supervisor.sock         # supervisord control socket
~/.vicegerent/mcp/vmcp-config.json        # generated + validated vMCP config
~/.vicegerent/mcp/kubeconfig-vicegerent.yaml  # kind --internal kubeconfig (mounted into the k8s workload)
~/.vicegerent/mcp/logs/                   # per-process logs
```
