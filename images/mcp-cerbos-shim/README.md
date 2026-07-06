# mcp-cerbos-shim

An [agentgateway](https://agentgateway.dev) **ExtMcp** policy server that authorizes
MCP tool calls against a [Cerbos](https://cerbos.dev) PDP. It is the connector that lets
agentgateway enforce **argument-level** authorization on MCP traffic; e.g. block an agent
from reading Kubernetes Secrets through a generic Kubernetes MCP server, while allowing
reads of non-secret resources.

## Authorization Layers

Tool *selection* (which tools an agent can see and call) and argument-level
*authorization* are separate concerns, handled by separate layers.

Tool selection here is done upstream in ToolHive's vMCP (`aggregation.tools` in
`toolhive-servers.json`): an operator scopes a backend's tools by editing that file
and restarting the host stack — no cluster round-trip. agentgateway can also enforce a
per-tool allowlist itself (its MCP tool-filtering feature); a corporate/centralized
deployment would put the allowlist there, version-controlled at the gateway. Which
place owns it is a policy choice — this platform keeps it in ToolHive for developer
flexibility.

This shim + Cerbos are an orthogonal layer: argument-level authorization that denies by
*resource* whatever the exposed tool set is — currently Kubernetes Secret reads,
OpenSearch Grafana datasource reads, Jira calls targeting a project other than CHANGE,
GitHub calls targeting a repo outside an allowlist or writing directly to a protected
branch, Linear `save_issue` calls (create or update) targeting a team other than DEVOPS,
Alertmanager silences over the configured duration cap or a `deleteSilence` on a silence
the bot didn't create, and PagerDuty calls outside the allowed service or bulk operations
over the configured cap. GitLab has no Cerbos-mapped tools or policy at all (its git
file/branch-write tools were dropped from the allowlist instead). (Notion `create-pages`
is also mapped, but as a `force` rewrite of its `parent` to the Scratchpad folder — a
mutation, not a deny; see "How it works".)

| Layer | Job |
| --- | --- |
| **agentgateway** | MCP ingress gate: routing, bearer auth, mTLS to the host vMCP. Can also enforce a per-tool allowlist centrally; in this setup that's left to ToolHive. |
| **mcp-cerbos-shim** (this) | Extract the resource a *resource-bearing* tool targets (a k8s kind, a Grafana datasource id, a Jira project/issue key, a GitHub owner/repo/branch, a Linear teamId, an Alertmanager silence duration/creator, or a PagerDuty service/incident) and ask Cerbos about it; apply any `force` arg-rewrite on allow. |
| **Cerbos policy** | Make the deny decision: block calls that touch Secrets, OpenSearch datasources, a non-CHANGE Jira project, a GitHub repo outside the allowlist or a protected-branch write, a non-DEVOPS Linear team, an over-cap/non-owner Alertmanager silence, or an out-of-service/bulk PagerDuty call, and reject a kind-bearing call whose kind can't be resolved. Allow-all for all roles + deny overrides for the protected resources and empty-kind. |

Consequences:
- A new tool exposed by the vMCP needs **no** shim/Cerbos change unless it can name a
  protected resource; otherwise it passes the guardrail. (Whether it's *exposed* is the
  separate tool-selection choice above.)
- An unknown/arbitrary Kubernetes kind (e.g. a CRD) passes the guardrail, not denied;
  the shim blocks Secrets, it does not enumerate readable kinds.
- The mapped tools are only the ones that name a protected resource: the k8s
  `kind`/resource selectors (`kubernetes_resources_get`, `kubernetes_resources_list`),
  the Grafana datasource-bearing tools (`grafana_get_datasource`,
  `grafana_query_prometheus*`, `grafana_list_prometheus_*` — `grafana_check_datasources_health`
  takes a plural `uids` array this single-resource-per-call model can't check, and only
  reveals up/down status rather than a datasource's actual data, so it's unmapped),
  the project/issue-bearing Jira tools
  (`jira_jira_create_issue`, `jira_jira_get_issue`, `jira_jira_update_issue`,
  `jira_jira_transition_issue`, `jira_jira_add_comment`, `jira_jira_get_transitions`,
  `jira_jira_get_project_issues`, `jira_jira_create_issue_link`,
  `jira_jira_link_to_epic`), every repo-bearing GitHub tool in the vMCP allowlist
  (`github_pull_request_read`, `github_create_pull_request`,
  `github_update_pull_request_branch`, etc. — full list in mapping.yaml;
  `github_get_me` is the one exception, since it names no repo). GitHub's tool
  set is deliberately PR-only: no issue tools (this operator doesn't use GitHub
  issues at work) and no generic git file/branch-write tools
  (`create_branch`/`create_or_update_file`/`push_files` — the bot has direct
  SSH access to github.com, so routine git operations go through git itself).
  GitLab is similarly trimmed of its three git file/branch-write tools
  (`push_files`/`create_or_update_file`/`create_branch` — same SSH-access
  rationale, gitlab.hahomelabs.com is also directly reachable over SSH) but is
  otherwise NOT scoped down like GitHub — the operator owns this GitLab
  instance and isn't picky about its issue/MR tools, which stay unmapped and
  allow-all. GitLab has no `resource_gitlab.yaml` policy or `gitlab_repo`
  Cerbos resource at all anymore — it was removed once nothing populated it.
  And Linear's `linear_save_issue` (`team` is required on create and
  always verifiable; on update the caller usually sends no `team` for the
  existing issue, so the shim leaves that call unmapped — but a call that
  DOES set `team` on an update, i.e. a deliberate reassignment, is checked
  exactly like a create). `linear_save_comment`/`linear_save_project` target
  an existing comment/project by id and carry no team of their own, so
  they're unmapped.
  `notion_notion-create-pages` is also mapped, but only to carry a `force`
  parent-rewrite (allow-all Cerbos policy); its siblings
  `notion_notion-update-page`/`notion_notion-create-comment` are unmapped (they
  target an existing page by id, not a folder). Everything else passes untouched.

The shim mapping and Cerbos rules exist to *deny* protected resources, not to permit
tools. To allow or disallow a tool outright, change the exposed tool set (ToolHive's
`aggregation.tools` today, or an agentgateway per-tool allowlist in a centralized
setup) — not the mapping or an `allow` rule here.

### Guardrail Attachment

The Secret block depends on `AgentgatewayPolicy` attaching the `tools/call ->
mcp-cerbos-shim` guardrail with `failureMode: FailClosed`. `FailClosed` only
covers invoked processor failures; a missing guardrail silently fails open.

- **Authoring (covered):** `scripts/validate.sh` renders the overlay and fails CI
  if the rendered `AgentgatewayPolicy` does not carry exactly one `tools/call ->
  mcp-cerbos-shim` guardrail with `FailClosed`. A bad edit can't merge.
- **Runtime (NOT covered):** if Flux never reconciles the commit, or the
  controller silently rejects the CRD, the live gateway can lack the guardrail
  even though the repo is correct. There is no backend-level default-deny in the
  agentgateway CRD to backstop this, so it is an accepted gap on this dev
  platform. Treat a reconcile failure on this policy as a security incident.

## Why it exists

agentgateway's HTTP `extAuthz` (which Cerbos speaks natively) **cannot see MCP tool
arguments** at decision time (upstream issue #720; the `mcp` CEL context is empty during
extauthz). Only the **ExtMcp guardrails** protocol (`ext_mcp.proto`) carries the tool name +
params, and Cerbos doesn't implement it. This connector bridges the two: it implements
`ExtMcp.CheckRequest` on one side and calls Cerbos `CheckResources` on the other.

## How it works

For each `tools/call` the gateway forwards (`McpRequest`), the connector:

1. Resolves the backend from `service_names` (exactly one mapped backend, else deny).
2. Parses the JSON-RPC params (`{name, arguments}`); unparseable/missing denies.
   If `name` is `call_tool` (the vMCP optimizer's meta-tool — see
   `host/mcp/README.md` "Tool discovery optimizer"), unwraps
   `arguments.{tool_name,parameters}` into the real tool/args first; a missing
   or non-string `tool_name` denies. Without this, every optimizer-routed call
   would look identical (`call_tool`) to every other, defeating step 3 below.
3. Looks up `(backend, tool)` in the mapping. The `vmcp` backend is `defaultAction:
   allow`, so an unmapped tool **passes** (it can't name a Secret); only the
   kind-bearing tools are mapped.
4. Evaluates the mapped tool's CEL expressions against `{tool, args, backend,
   method}` to build a Cerbos resource (standardizing kind/apiResource via the
   `canonicalK8s` helper). A CEL eval failure denies (the shim's own
   malfunction; never send a half-built resource).
5. Calls Cerbos `CheckResources` (via the `authz.Decider` interface). Denied or Cerbos error
   returns `AuthorizationError`; the deny reason is the matched rule's policy-authored `output`
   (see `policies/defs/*.yaml` `output:` blocks, e.g. `deny-self-approve`'s "use REQUEST_CHANGES
   instead") when the rule has one configured, falling back to a generic backend-agnostic
   message when it doesn't. This is what lets a calling agent understand *why* it was blocked and
   self-correct instead of retrying blind or silently downgrading its own intent — see HAH-65/72.
   Allowed returns `Pass{}` — unless the tool's mapping carries a `force` block (literal key/value
   overrides, e.g. GitHub PR create/update forcing `draft: true`), in which case it returns
   `Mutated{}` with those keys rewritten into the call's arguments (re-wrapped into
   `call_tool{tool_name,parameters}` first if the call arrived that way). `force` only ever
   applies on an allowed call — a denied call is never mutated.

It never returns `header_mutation` or `metadata` (the gateway applies `metadata` even on
`Pass`, so leaving it empty is part of the contract); `mutated` is set only for a `force`-mapped
tool on allow, otherwise it's `pass` or `error`.

The shim makes **no policy decisions**; it standardizes fields and delegates
the verdict, with one narrow exception: a `force` block is a fixed, unconditional rewrite
(never derived from the call's own args), not a judgment call. Every *deny* is Cerbos's
(Secrets, and a kind-bearing call whose kind can't be resolved as `kind==""`/`deny-no-kind`);
everything else passes the guardrail by default — tool selection is handled upstream
(ToolHive, or an agentgateway allowlist in a centralized setup), not here. The shim only
fails closed on its own malfunction (unparseable params, unknown/multiple backend, CEL eval
error, Cerbos unreachable, or a force-mapped tool's args failing to re-serialize).
See `internal/server/server_test.go` for the full matrix.

## Config

A YAML mapping (see `mapping.example.yaml`) mounted at `MAPPING_PATH`. Every `id`/`attr`/
`attrFrom` value is a CEL expression compiled and type-checked at startup; an invalid mapping
aborts startup (k8s restarts the pod; the gateway's `FailClosed` denies meanwhile).

| Env var | Default | Meaning |
| --- | --- | --- |
| `LISTEN_ADDR` | `:4445` | gRPC listen address |
| `MAPPING_PATH` | `/etc/mcp-cerbos-shim/mapping.yaml` | mapping file |
| `CERBOS_ADDR` | `cerbos.cerbos.svc.cluster.local:3593` | Cerbos PDP gRPC |
| `CERBOS_PLAINTEXT` | `true` | use plaintext to the PDP (mTLS later) |

The Cerbos principal is a fixed constant (`hermes`/`agent`) stamped on every
request for audit context. It is **not** an authorization control: the policy
allows all roles and denies only by resource, so there is nothing to configure.

## Helpers (per-backend, pluggable)

The CEL evaluator core is generic; it knows the MCP wire shape and Cerbos, nothing
about any specific server. Server-specific normalization lives in **helpers**: CEL
functions a mapping opts into via a backend's `helpers:` list, scoped to that backend
only (a k8s helper can't leak into a GitHub or AWS mapping).

Built-in:

| Helper | Backend | Purpose |
| --- | --- | --- |
| `get(map, key, default)` | core (always in scope) | case-insensitive arg lookup |
| `canonicalK8s(args)` | `helpers_k8s.go` | reads `kind`/`Kind` case-insensitively and normalizes a **Secret** reference (plural, `v1/secrets`, etc.) to `{kind:Secret, apiResource:secrets}` so Cerbos's deny-secrets rule catches every spelling; any other kind is passed through unchanged (no per-kind allowlist) |
| `linearIssueAttr(args)` | `helpers_linear.go` | surfaces `teamId` from `save_issue`'s `team` arg only when it's verifiable — always on create (no `id` arg), or on update only if the call itself sets `team` — otherwise omits the key entirely so an ordinary update falls through to allow-all instead of tripping Cerbos's `has()`-based deny on an empty value |

To add a helper for a new MCP server, drop an `internal/eval/helpers_<backend>.go`
whose `init()` calls `registerHelper("<name>", <ctor>)` and defines the CEL function;
**no edits to the generic core**. Reference it from that backend's mapping under
`helpers:`. A name listed in a mapping but not registered aborts startup (fail closed);
duplicate registration panics at startup.

## Build

A `Makefile` wraps the common tasks (`make help` lists them). The image name is
fixed to `harbor.hahomelabs.com/vicegerent/mcp-cerbos-shim`; override the tag
with `TAG=`.

```bash
make check                 # gofmt-check + go vet + go test ./... (CI parity)
make image TAG=v0.1.0      # docker build
make push  TAG=v0.1.0      # docker push to Harbor
make release TAG=v0.1.0    # check + image + push
make proto                 # regenerate stubs (only when proto/ext_mcp.proto changes)
```

`make proto` requires the protoc plugins on PATH:

```bash
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

`proto/ext_mcp.proto` is vendored from agentgateway and pinned to the deployed gateway
version (v1.3.1). The proto is not API-stable across versions; bump it deliberately.
