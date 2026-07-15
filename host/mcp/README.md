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
  -> 17 ToolHive workloads (group 'vicegerent')
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

## The 17 backends (group `vicegerent`)

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
| `elastic` | remote transport to Kibana Agent Builder (URL param) | `elastic_kibana_url` + `elastic_api_key` secrets |
| `aws` | custom `harbor.hahomelabs.com/vicegerent/aws-api-mcp-server` (non-blocking patch of upstream awslabs image, read-only) | read-only `~/.aws` mount (SSO), multi-profile |
| `aws_profiles` | custom `harbor.hahomelabs.com/vicegerent/aws-profiles-mcp` (hidden companion of `aws`) | read-only `~/.aws` mount |

Tool scoping uses the vMCP's native `aggregation.tools` primitive: a server
with a `tools` allowlist in `toolhive-servers.json` emits a `{workload, filter}`
entry so the vMCP exposes only those tools (raw, unprefixed names). Every
backend now carries one — for `tavily`/`firecrawl` it's the full live tool set
pinned explicitly (nothing to restrict — tavily/firecrawl have no write
capability against anything this platform owns; pinning just stops a future
package bump from silently adding to what's exposed), for `alertmanager` it's
the full 12-tool set including `createSilence`/`deleteSilence` (an explicit
choice, not an oversight — the operator wants the agent able to manage
silences), and for `elastic` it's the 24 read/analysis tools (the 3 write
tools are excluded). The rest genuinely restrict:

- `kubernetes` — read-only at the source (`--read-only` makes writes
  impossible regardless of allowlist), and the allowlist additionally excludes
  `configuration_view`: despite being `readOnlyHint=true`, it returns the full
  kubeconfig including `client-certificate-data`/`client-key-data` in
  plaintext — a live cluster credential handed straight into agent context and
  transcripts. `configuration_contexts_list` (names + server URLs only, no key
  material) covers "what clusters/contexts exist" instead.
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
- `elastic` — the 24 read/analysis tools (streams, core search/ES|QL/index,
  security, observability); the 3 write tools (create-visualization,
  create-detection-rule, resume-workflow-execution) are excluded. The Cerbos
  guardrail below additionally denies any data-access call targeting a blocked
  index/datastream token.

Doing tool selection here (rather than as a per-tool allowlist in agentgateway,
which it also supports) keeps it a quick host-side edit for developers; a
centralized corporate deployment would more likely enforce that allowlist at the gateway.

### Network egress lockdown

Every backend also carries a `network` block in `toolhive-servers.json`,
enforced via ToolHive's native `--permission-profile` mechanism (network
isolation is ToolHive's default since v0.30.1 — no `--isolate-network` flag
needed to turn it on). `build_permission_profile()`/`write_permission_profile()`
in `vicegerent_mcp.py` turn that config into a per-server JSON profile
(`network.outbound.allow_host`/`allow_port`) written to the runtime dir and
passed as `--permission-profile <path>` at `thv run` time, so each container's
egress is locked to exactly the hosts it needs — anything else is denied by
ToolHive's own egress proxy, independent of and in addition to the Cerbos/tool
allowlisting above.

`network` takes one of these shapes:

- **`allow_hosts: [...]`** — static hostnames, safe to hardcode because they
  don't vary across users/clusters (fixed cloud endpoints): `github`
  (github.com, api.github.com), `notion` (mcp.notion.com — the official hosted
  remote), `linear` (mcp.linear.app — the official hosted remote), `tavily`
  (api.tavily.com), `firecrawl` (api.firecrawl.dev), `pagerduty`/`pagerduty_gov`
  (api.pagerduty.com — the PagerDuty MCP server's own docs confirm this is the
  only host used unless `PAGERDUTY_API_HOST` is overridden for an EU account,
  which this config doesn't do for either workload), and `aws`
  (`.amazonaws.com`, `.api.aws`). The leading dot is a suffix match — ToolHive's
  egress proxy is squid and turns each `allow_hosts` entry into an
  `acl … dstdomain <host>`, so a leading-dot entry covers every subdomain: one
  `.amazonaws.com` allows every `<service>.<region>.amazonaws.com` (all regions
  incl. gov, plus the SSO oidc/portal and read-only-classification endpoints)
  without enumerating them, and `.api.aws` covers newer service endpoints + the
  `suggest_aws_commands` endpoint. Verified via the egress proxy's access log.
- **`host_from_param: "<param name>"`** — the hostname is parsed (via
  `urllib.parse.urlparse`) out of a `params[]` entry's *resolved* value at `thv
  run` time — never hardcoded, since it's per-operator/per-cluster. Covers
  `gitlab` (its `api_url` param) and `alertmanager`/`alertmanager_gov` (their
  `url` param), and `elastic` (its `kibana_url` param). Raises a
  clear error (same pattern as the existing kubeconfig check) if
  `./vicegerent mcp configure` hasn't set that param yet.
- **`host_from_secret: "<thv secret name>"`** — same idea, but for a hostname
  that lives in a top-level `secrets[]` entry instead of `params[]` (fetched
  directly via `thv secret get`). Covers `jira` (`jira_url`) and
  `grafana`/`grafana_gov` (`grafana_url`/`grafana_gov_url`).
- **`exempt: true`** — out of scope for permission-profile allowlisting
  entirely. Only `kubernetes`: it already opts out of ToolHive's network
  isolation via `--isolate-network=false` (see "Kubernetes networking" below)
  because it needs raw docker-network TCP to the kind API server, which the
  egress proxy (HTTP/HTTPS only) can't front.
- **`none: true`** — deny-all egress (a permission profile with an empty
  allow-list). Only `aws_profiles`: it makes no outbound calls, just reads the
  mounted `~/.aws/config` and serves stdio through ToolHive's bridge.

A change to `network` (a new allowlisted host, an edited hostname param) is
folded into `server_spec_fingerprint()`'s drift-detection hash, so `start`
recreates the affected workload instead of leaving a stale `--permission-profile`
baked into an already-running container. The same fingerprint also folds in the
CONTENT of a server's mounted host config (the `aws`/`aws_profiles` `~/.aws`
directory, a user-supplied kubeconfig): an `aws sso login`, a newly added
profile, or an edited kubeconfig changes the hash, so the next `start` recreates
the workload with the fresh config rather than relying on the live bind mount
(some servers read their config only once at startup). The kind-cluster internal
kubeconfig's CA rotation is handled separately by `kind_kubeconfig_stale`.

`aws_profiles` is a **hidden companion** of `aws` (`companion_of: aws`): it's
enabled/disabled with `aws` as one unit, never shown or configured on its own
(so a developer needn't know it exists), and inherits `aws`'s `~/.aws` mount
config. Its sole tool (`aws_profiles_list`) lets the agent discover which
`--profile` values `call_aws` accepts — the `aws` backend can't enumerate
profiles itself.

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
- **GitLab** has no Cerbos-mapped tools and no `resource_gitlab.yaml` policy
  at all. `push_files`/`create_or_update_file`/`create_branch` (its only
  branch-writing tools) were removed from the tool allowlist entirely — the
  bot has direct SSH access to gitlab.hahomelabs.com, so routine git
  operations go through git itself, not a GitLab-API tool. The operator owns
  this GitLab instance and isn't picky about its remaining tools (issues, MR
  object/comments/discussions/notes/labels/todos/pipelines), which stay
  unmapped and allow-all.
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

With 17 backends aggregated, the raw tool count is large enough to burn a
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
thv secret set elastic_kibana_url    # Kibana Agent Builder MCP URL, e.g. https://<your-kibana-host>/api/agent_builder/mcp
thv secret set elastic_api_key       # read-only Elastic API key (Stack Management > Security > API keys)
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
