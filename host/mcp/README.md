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
  -> 11 ToolHive workloads (group 'vicegerent')
```

`thv` runs the workloads as Docker containers under ToolHive's own daemon —
they persist across `start`/`stop` so OAuth tokens are not re-prompted. `start`
detects when a workload's declared spec (package, env, run/server flags, secret
targets) has drifted from what's actually running — e.g. editing `env` or
`tools` for a server already up — and recreates that one container instead of
`thv restart`-ing it (which would silently keep the OLD args forever, since
restart reuses whatever was passed to the container's original `thv run`).
Supervisord manages only the three long-lived host processes:

- `caffeinate` — keeps macOS awake for as long as the stack is up.
- `vmcp` — `thv vmcp serve` aggregating the group on 127.0.0.1:4483.
- `ghostunnel` — terminates mTLS from the cluster (client CN `agent-client`)
  and forwards to vMCP.

## The 11 backends (group `vicegerent`)

The workload name is the vMCP tool prefix, and the Cerbos policy keys off it —
these names are load-bearing, defined in `toolhive-servers.json`:

| Workload | Run | Auth |
|---|---|---|
| `kubernetes` | npx `kubernetes-mcp-server` (`--read-only`) | kind `--internal` kubeconfig |
| `gitlab` | npx `@zereight/mcp-gitlab` | `gitlab_token` secret |
| `github` | registry `io.github.stacklok/github` (container) | `github_token` secret |
| `tavily` | npx `tavily-mcp` | `tavily_api_key` secret |
| `firecrawl` | npx `firecrawl-mcp` | `firecrawl_api_key` secret |
| `notion` | registry remote `notion-remote` | OAuth (browser, first run) |
| `linear` | registry remote `linear` | OAuth (browser, first run) |
| `jira` | registry `io.github.stacklok/atlassian` (sooperset container) | `jira_url` + `jira_username` + `jira_api_token` secrets |
| `grafana` | registry `grafana` (container) | `grafana_url` + `grafana_service_account_token` secrets |
| `alertmanager` | npx `mcp-alertmanager` | `url` param (`--url`), no secret |
| `pagerduty` | registry `io.github.stacklok/pagerduty` (container) | `pagerduty_user_api_key` secret |

Tool scoping uses the vMCP's native `aggregation.tools` primitive: a server
with a `tools` allowlist in `toolhive-servers.json` emits a `{workload, filter}`
entry so the vMCP exposes only those tools (raw, unprefixed names). Every
backend now carries one — for `kubernetes`/`tavily`/`firecrawl` it's the full
live tool set pinned explicitly (nothing to restrict — `--read-only` already
makes k8s writes impossible at the source, and tavily/firecrawl have no write
capability against anything this platform owns; pinning just stops a future
package bump from silently adding to what's exposed), for `alertmanager` it's
the full 12-tool set including `createSilence`/`deleteSilence` (an explicit
choice, not an oversight — the operator wants the agent able to manage
silences). The rest genuinely restrict:

- `pagerduty` — incidents R/W + read-only schedules/services/teams/users/escalation.
- `grafana` — read-only search/datasource/dashboard/prometheus/asserts/annotations/rendering.
- `jira` — read+write only, Confluence disabled, deletes excluded, confined to
  the CHANGE project via `JIRA_PROJECTS_FILTER`; also scoped at the source via `ENABLED_TOOLS`.
- `github` — issues + the full PR lifecycle short of merging; create_repository/
  fork_repository/delete_file/merge_pull_request and general repo/code browsing
  excluded; also scoped at the source via `GITHUB_TOOLS`.
- `gitlab` — issues + the full MR lifecycle short of merging; create_repository/
  create_group/fork_repository/merge_merge_request, all CI/CD variable and
  pipeline-trigger tools, `execute_graphql` (an arbitrary API-access escape hatch
  that would make every other exclusion here meaningless), and wiki/release/tag/
  milestone/webhook management are excluded.
- `linear` — Linear's real surface has grown to 55 tools, including 5 destructive
  deletes and several newer feature categories (attachments, releases, milestones,
  customers, initiatives, status updates); this allowlist keeps the original
  functional scope (issues/comments/projects/labels/statuses/teams/users/docs,
  read+write, no deletes) via the renamed save_issue/save_comment/save_project
  tools, and excludes the rest pending a deliberate follow-up. The Cerbos
  guardrail below confines save_issue's team to one team instead.
- `notion` — 7 read tools plus create-pages/update-page/create-comment.

Doing tool selection here (rather than as a per-tool allowlist in agentgateway,
which it also supports) keeps it a quick host-side edit for developers; a
centralized corporate deployment would more likely enforce that allowlist at the gateway.

Orthogonal argument-level authorization still lives in the cluster (the Cerbos
guardrail on the `vmcp` backend); no Cedar/authz runs in the vMCP.

- **GitHub** (`defs/resource_github.yaml`) hard-enforces an owner/repo allowlist
  and a protected-branch block (main/master/production) on every mapped tool —
  independent of the source-side scoping above, and unaffected by however broad
  the underlying PAT's own access actually is. `create_pull_request`/
  `update_pull_request` also carry a shim-side `force: {draft: true}` — every PR
  the agent opens, or tries to un-draft via update, is rewritten to stay a draft
  before it's forwarded (a mutation, applied only once Cerbos has already
  allowed the call).
- **GitLab** (`defs/resource_gitlab.yaml`) blocks `push_files`/
  `create_or_update_file`/`create_branch` from targeting a protected branch.
  No project allowlist — the bot's GitLab PAT is already scoped to a single
  project on gitlab.hahomelabs.com, so the token itself is the repo boundary
  (deliberately different from GitHub, whose token is broader).
- **Linear** (`defs/resource_linear.yaml`) denies a `save_issue` call that
  supplies a `team` other than DEVOPS (matched by uuid, display name, or issue-key
  prefix). `save_issue` merges Linear's old create_issue/update_issue into one
  tool (an `id` arg picks update vs create); `team` is required on create and
  optional on update, so an ordinary update that omits it isn't checked — the
  enforced boundary is "new issues land in DEVOPS, and no issue can be
  reassigned off it," not "every call touches only DEVOPS."
  `save_comment`/`save_project` target an existing comment/project by id and
  carry no verifiable team of their own, so they're unmapped and pass.
- **Notion** (`defs/resource_notion.yaml`) folder-pins `create-pages` via a
  shim-side `force` override — it rewrites `parent` to the Scratchpad page on
  every call rather than denying an off-folder parent, so the agent never sees
  an error or retries and every new page lands under Scratchpad;
  `update-page`/`create-comment` target an existing page by id and are left
  unconstrained (a hard read-broad/write-narrow split via a separate
  folder-scoped integration was infeasible — the org blocks creating Notion
  internal-integration tokens).

`kubernetes`'s Secret-read block (`defs/resource_k8s.yaml`) and `grafana`'s
OpenSearch-datasource block (`defs/resource_grafana.yaml`) are the other two guardrails.

One field-name assumption in the GitLab mapping isn't verified against a live
call yet (unlike GitHub/Jira, where a real schema dump was available): GitLab's
`branch` field on push_files/create_or_update_file/create_branch is inferred
from GitLab's own REST API convention (which this wrapper mirrors directly).
Confirm it once the server is enabled. Linear's `save_issue` schema (`team`,
not `teamId`) was confirmed against a live `tools/list` call.

### Tool discovery optimizer

With 11 backends aggregated, the raw tool count is large enough to burn a
meaningful chunk of the agent's context budget just listing tool definitions.
`thv vmcp serve --optimizer` (Tier 1, FTS5 keyword search, no extra container)
collapses the exposed surface to two meta-tools — `find_tool` (search) and
`call_tool` (invoke by name) — so the agent discovers tools on demand instead
of loading all of them up front. It's on by default (`generate_vmcp_config`'s
caller passes `--optimizer`); set `VMCP_OPTIMIZER=0` before `./vicegerent-mcp
start` to fall back to exposing every tool raw.

This requires `mcp-cerbos-shim` to unwrap `call_tool`'s wrapped
`{tool_name, parameters}` back into the real tool name before its mapping/Cerbos
lookup (see `images/mcp-cerbos-shim/README.md` "How it works") — without that,
every optimizer-routed call looks identical to the shim and the Secret/OpenSearch/
Jira-project guardrails would silently stop applying. Don't enable `--optimizer`
against an older shim image that predates this unwrap.

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
thv secret set github_token         # GitHub PAT (repo scope)
thv secret set tavily_api_key
thv secret set firecrawl_api_key
thv secret set grafana_url                       # e.g. https://grafana.example.com
thv secret set grafana_service_account_token     # Grafana service-account token
thv secret set jira_url                          # e.g. https://your-domain.atlassian.net
thv secret set jira_username                      # Jira account email (Cloud)
thv secret set jira_api_token                     # Jira API token (id.atlassian.com/manage-profile/security/api-tokens)
```

`notion` `create-pages` is pinned to the **Scratchpad** page by the shim's `force`
override in `infrastructure/controllers/mcp-cerbos-shim/mapping.yaml`: every create is
rewritten to land under that page (no error, no retry). Change the forced `parent.page_id`
there (32 hex, dashes stripped, lowercase) to retarget.

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

`toolhive-servers.json` declares the group, the vMCP port, and the 10 servers
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
