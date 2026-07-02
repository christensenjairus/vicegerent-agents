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
  -> 9 ToolHive workloads (group 'vicegerent')
```

`thv` runs the workloads as Docker containers under ToolHive's own daemon —
they persist across `start`/`stop` so OAuth tokens are not re-prompted.
Supervisord manages only the three long-lived host processes:

- `caffeinate` — keeps macOS awake for as long as the stack is up.
- `vmcp` — `thv vmcp serve` aggregating the group on 127.0.0.1:4483.
- `ghostunnel` — terminates mTLS from the cluster (client CN `agent-client`)
  and forwards to vMCP.

## The 9 backends (group `vicegerent`)

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
| `grafana` | registry `grafana` (container) | `grafana_url` + `grafana_service_account_token` secrets |
| `alertmanager` | npx `mcp-alertmanager` | `url` param (`--url`), no secret |
| `pagerduty` | registry `io.github.stacklok/pagerduty` (container) | `pagerduty_user_api_key` secret |

Tool scoping uses the vMCP's native `aggregation.tools` primitive: a server
with a `tools` allowlist in `toolhive-servers.json` emits a `{workload, filter}`
entry so the vMCP exposes only those tools (raw, unprefixed names); a server
without one exposes everything. Currently `pagerduty` (incidents R/W + read-only
schedules/services/teams/users/escalation) and `grafana` (read-only
search/datasource/dashboard/prometheus/asserts/annotations/rendering) are
scoped this way. Instance-level authorization still lives in the cluster
(agentgateway + the Cerbos guardrail); no Cedar/authz runs in the vMCP.

### Kubernetes networking (the gotcha)

`thv` containerizes the npx k8s server, so it lives in the Docker VM and cannot
reach a host-loopback API. The container is attached to the kind docker network
(`--network vicegerent --isolate-network=false`) and fed kind's `--internal`
kubeconfig (`kind get kubeconfig --name vicegerent --internal`, server
`https://vicegerent-control-plane:6443`), mounted read-only and pointed at via
`KUBECONFIG` + `--kubeconfig`.

## Security & trust boundary (read before running on a shared machine)

The host side of this stack trusts the host. Two exposures are inherent to how
Docker Desktop + ToolHive work today — know them before running untrusted
containers alongside the stack:

- **The vMCP (`127.0.0.1:4483`) is anonymous and reachable from a *sibling Docker
  container* on the Mac — but NOT from inside the cluster.** The Cilium egress-lock
  holds the cluster side: only `agentgateway-proxy` gets any host egress, scoped to
  `192.168.65.0/24:8453`, so even it is denied `:4483`, and the agent sandbox is
  denied both ports (verified live — every in-cluster path to the vMCP is forced
  through ghostunnel's mTLS on `:8453`). The residual gap is outside the CNI: on
  Docker Desktop, `host.docker.internal` resolves to the host loopback for *every*
  container and the proxy doesn't filter by which container connects, so
  `docker run alpine wget host.docker.internal:4483/...` reaches the vMCP directly,
  bypassing ghostunnel. This is a host-trust assumption (don't run a hostile
  container on this Mac while the stack is up), not an agent-isolation escape.
  There is no cheap fix in thv v0.33: `incomingAuth` accepts only `anonymous` or
  `oidc` (no bearer token), the vMCP is TCP-only (no Unix socket), and macOS
  loopback has only `127.0.0.1` (rebinding to another loopback needs a `sudo`
  alias). Closing it fully means OIDC incoming auth or upstream vMCP bearer/UDS
  support.

- **Enabling the `kubernetes` workload widens the trust boundary to the whole
  `vicegerent` docker network.** That workload runs with `--network vicegerent`
  (to reach the node's in-network API), and that bridge is flat: any container on
  it can raw-TCP the kind node's kubelet (`:10250`), apiserver (`:6443`), and every
  dashboard NodePort (`30119–30128`) by the node's docker IP — bypassing the
  `127.0.0.1` `extraPortMappings` restriction (which only governs host↔container).
  None of this is visible to Cilium or the Cerbos guardrail (they only see
  in-cluster / agentgateway traffic). Backstops: the apiserver is TLS+RBAC-gated and
  the kubeconfig is `--read-only`; the kubelet has anonymous-auth off (Kind default);
  the dashboards require basic auth (mandatory — the password Secret is
  `optional: false`). It is off by default; enabling it is an informed choice.

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
thv secret set grafana_url                       # e.g. https://grafana.example.com
thv secret set grafana_service_account_token     # Grafana service-account token
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

`toolhive-servers.json` declares the group, the vMCP port, and the 9 servers
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
