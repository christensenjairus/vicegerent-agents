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
Alertmanager `createSilence` calls over the configured duration cap (`deleteSilence` has
no ownership-based deny — see `defs/resource_alertmanager.yaml`), PagerDuty
`manage_incidents` calls that change anything other than status=acknowledged/resolved,
Notion `update-page` calls that set `command: replace_content` or
`allow_deleting_content: true` (and, via a shim-side live ancestry check, any `update-page`
targeting a page not under the agent's Scratchpad tree — see "How it works"), and Firecrawl
`firecrawl_interact` calls that carry raw `code` to execute in the browser session
(`prompt`-only interaction remains allowed). Notion `create-pages` calls whose parent isn't
the Scratchpad folder are also denied (a deny, not a mutation — see below).
GitLab has no Cerbos-mapped tools or policy at all (its git file/branch-write tools were
dropped from the allowlist instead). (Notion `create-pages` is also mapped; see
"How it works".)

| Layer | Job |
| --- | --- |
| **agentgateway** | MCP ingress gate: routing and mTLS to the host vMCP. Can also enforce a per-tool allowlist centrally; in this setup that's left to ToolHive. |
| **mcp-cerbos-shim** (this) | Extract the resource a *resource-bearing* tool targets (a k8s kind, a Grafana datasource id, a Jira project/issue key, a GitHub owner/repo/branch, a Linear teamId, an Alertmanager silence duration, a PagerDuty manage_incidents change, a Notion update-page command, or a Firecrawl interact code payload) and ask Cerbos about it; apply any `force` arg-rewrite on allow. |
| **Cerbos policy** | Make the deny decision: block calls that touch Secrets, OpenSearch datasources, a non-CHANGE Jira project, a GitHub repo outside the allowlist or a protected-branch write, a non-DEVOPS Linear team, an over-cap Alertmanager silence, an out-of-scope PagerDuty change, a destructive Notion update-page command, or a code-carrying Firecrawl interact call, and reject a kind-bearing call whose kind can't be resolved. Allow-all for all roles + deny overrides for the protected resources and empty-kind. |

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
  `notion_notion-create-pages` is mapped to a Cerbos deny on any parent other
  than the Scratchpad folder (`defs/resource_notion.yaml`'s
  `deny-create-outside-scratchpad` rule), and `notion_notion-update-page` is
  mapped both to the destructive-command Cerbos deny AND to a shim-side live
  ancestry gate that denies updates to any page not under the
  Scratchpad tree. `notion_notion-create-comment` shares that same live
  ancestry gate (it targets an existing page by id too, with no
  destructive-command surface of its own). Everything else passes untouched.

The shim mapping and Cerbos rules exist to *deny* protected resources, not to permit
tools. To allow or disallow a tool outright, change the exposed tool set (ToolHive's
`aggregation.tools` today, or an agentgateway per-tool allowlist in a centralized
setup) — not the mapping or an `allow` rule here.

### Content Moderation

A third, orthogonal concern sits alongside tool selection and argument-level
authorization: content **quality**, not authorization. Every rule described
above checks WHO/WHERE a call targets (repo, team, parent page, service id) —
none of them inspect WHAT a call's free text actually says. A hallucinated
claim about a person, a wrong incident-attribution narrative, or plainly
offensive content in a shared Notion/Linear workspace or a merged GitHub PR
description was previously uncatchable by any layer of this platform.

When enabled (`CONTENT_MODERATION=enabled`, toggled per-cluster via the
`contentModeration` cluster-var), the shim
sends every free-text string argument of a matching write call through
OpenAI's Moderations endpoint (reused `openai` AgentgatewayBackend, no new
secret or egress rule) BEFORE Cerbos is consulted, and denies outright on a
flagged result. "Matching write call" is a **verb heuristic** on the tool
name (`create`/`update`/`save`/`add_note`/`add_comment`/`transition` as a
case-insensitive substring — `DefaultModeratedWriteVerbs`/
`MODERATION_WRITE_VERBS`), not a hand-enumerated tool list: a newly added or
upstream-renamed write tool is covered automatically, at the cost of also
matching some non-text writes (harmless — extracting zero free-text strings
from those args is a zero-cost no-op, see `checkModeration`). Deliberately
excludes a bare "comment" substring (would false-match read tools like
`notion-get-comments`) in favor of the more specific "add_comment"/
"create_comment" forms actual write tools use. `transition` covers Jira's
`transition_issue`, whose optional comment ("resolve with comment: ...")
wouldn't otherwise match any other verb here.

This check runs on the tool name/args alone, **independent of whether the
tool has a Cerbos mapping entry in `mapping.yaml`** — GitLab, which has no
Cerbos-mapped tools or policy at all (see "Authorization Layers" above),
still gets its `create_issue`/`update_merge_request`/etc. write calls
checked. Placing this ahead of the mapping lookup is deliberate: tying it to
Cerbos-mapping status would silently exempt every unmapped backend from
moderation regardless of what its tool names matched.

**Why the cluster-var is `enabled`/`disabled`, never `true`/`false`:** Flux's
`postBuild.substituteFrom` does a raw text `${var}` replace on
already-rendered manifest bytes, and kustomize's own YAML emitter strips
quotes around this token regardless of source formatting — so a bare
`true`/`false` re-parses as a YAML boolean, not the string Kubernetes'
typed `EnvVar.Value` requires (confirmed byte-for-byte; this broke the work
cluster's rollout once already). `enabled`/`disabled` aren't YAML 1.1
bool/null/numeric tokens, so they always stay strings.

**Fail-open exception (unlike every other gate in this file):** a
moderation-service error (network failure, non-200, malformed response)
allows the call through rather than denying it. Every other gate here fails
closed on its own malfunction because its failure mode is "an authz check
couldn't be verified," which this platform treats as unsafe-by-default; a
moderation-endpoint hiccup is a service degradation, not an authz gap, and
Cerbos's own (unaffected) authz check still runs regardless — denying every
Notion/Linear/GitHub/GitLab/Jira/PagerDuty write cluster-wide on an OpenAI
outage is a worse outcome than the pre-existing no-content-check status quo.

**Known false-positive risk (precedent: MR !426):** agentgateway's own
promptGuard `builtins: [Ssn, CreditCard, PhoneNumber]` shorthand
self-rejected ordinary numeric content in production twice because of
unscored low-confidence patterns. OpenAI's Moderations endpoint is a
different mechanism (a real classifier, not a regex), but the same
"verify against real traffic before calling it done" discipline applies —
this was checked against live traffic before being considered stable; see
the HAH-106 MR description for that verification.

### Prompt Injection Detection

A fourth, orthogonal concern: untrusted content flowing INTO the agent's
context from a tool READ result -- a scraped Firecrawl/Tavily page, a fetched
Notion/Jira/Confluence page body, a GitHub file, a GitLab merge-request
diff -- can itself carry an injection payload ("ignore previous
instructions", "you are now in developer mode", ...) aimed at the agent
reading it. This is the mirror-image problem to Content Moderation above:
that gate checks free-text arguments of WRITE calls flowing OUT before
Cerbos is consulted; this one checks free-text in READ results flowing IN,
after the call already happened, since there's nothing to deny on the way
out for a read. That's also why it hooks `CheckResponse` and not
`CheckRequest` -- the content of concern doesn't exist until the response
comes back.

When enabled (`PROMPT_INJECTION_DETECTION=enabled`, toggled per-cluster via
the `promptInjectionDetection` cluster-var), the shim runs a **two-stage**
gate over every `redactableResponseMethods` response body (the same
`tools/call`/`resources/read`/`prompts/get` set secret redaction already
covers) and **DENIES** the call on a confirmed detection -- this deliberately
goes further than the Linear ticket's own suggested log-only first pass
(HAH-107), a decision made specifically because the two-stage design exists
to control the false-positive rate that would otherwise make blocking unsafe
(see MR !426's precedent below for why that risk is real).

- **Stage 1** (`internal/promptinjection`'s regex registry) is deliberately
  broad/high-recall -- expected to over-match benign content (e.g. a security
  blog post discussing injection attacks) -- and exists ONLY to decide
  whether stage 2 runs at all. A stage-1-only match never blocks by itself.
- **Stage 2** (`internal/promptinjection.Judge`, an LLM-judge call through
  the same `agentgateway` -> `openai` `/v1/chat/completions` route
  `internal/moderation` already uses -- no new secret or egress rule) runs
  ONLY on the text stage 1 flagged, asking a strict yes/no: is this an actual
  injection attempt, or text that merely discusses/documents one? This is
  the cost-control mechanism -- most reads never trigger it, since stage 1's
  broad net only rarely fires in ordinary traffic. A confirmed (`yes`)
  verdict denies the call outright (deny only, never a partial mutation/
  strip, mirroring Content Moderation's "no safe partial fix" posture); a
  clear `no`, or the same call succeeding with an ambiguous/unparseable
  reply, passes through unblocked. A stage-2 SERVICE error (timeout,
  non-200, network error) fails OPEN -- an unrelated OpenAI outage
  shouldn't deny every matching read cluster-wide -- distinct from a
  successful call that simply doesn't confirm a detection.

MR !426's precedent (agentgateway's own `promptGuard` self-rejecting
ordinary content on an under-verified regex gate) is exactly the failure
mode a *regex-only* blocking gate would risk -- which is why stage 1 alone
never blocks, and why stage 2's judge confirmation exists before this gate
denies anything. Every stage-1 match is logged regardless of the stage-2
outcome (pattern name, backend, judge verdict/error), so there's still a
full debug trail even though this gate now enforces.

Two bounds keep the judge cost/latency finite even against an
attacker-controlled response: stage 1 reports every occurrence of a
matched pattern (not just the first -- a real injection later in the same
document must not be able to hide behind an earlier, judged-benign
occurrence of the same pattern name), and `maxJudgeCallsPerResponse` caps
the TOTAL judge calls one `CheckResponse` invocation will make across every
matched occurrence. Exhausting that budget with unverified matches still
remaining is itself treated as a deny -- a response cheap enough to
synthesize that many candidate matches is a strong signal on its own, and
passing the unverified remainder through would reopen the same fan-out
bypass the cap exists to close.

Uses the same `enabled`/`disabled` cluster-var convention as
`contentModeration` (see that section above for why never `true`/`false`).

### Guardrail Attachment

The Secret block and the secret-redaction gate both depend on `AgentgatewayPolicy`
attaching the `tools/call -> mcp-cerbos-shim` guardrail with `methods: {tools/call:
Full}` and `failureMode: FailClosed`. `Full` is the `MCPMethodPhase` enum value that
routes BOTH `CheckRequest` and `CheckResponse` through the shim; the other values are
`Off`, `Request` (request-phase only), and `Response` (response-phase only). As of
HAH-101, `resources/read` and `prompts/get` are ALSO routed to the shim, but at phase
`Response` only -- neither has a Cerbos mapping to build an authorizable resource
from (no resource URI/prompt-name -> kind mapping exists), so there's no request-side
check to run for them; they exist purely to get their response bodies through
`CheckResponse`'s secret redaction (`redactableResponseMethods` in server.go), the
same gap `tools/call` results already had closed. Setting
`Request` instead of `Full` compiles, deploys, and passes `scripts/validate.sh`'s
render check with no error — it silently means `CheckResponse` (and therefore
response-side secret redaction — see "Building a redact-and-mutate gate" in the
`vicegerent-cerbos-guardrails` skill) never runs at all, agentgateway just never calls
it. This exact gap shipped to production once: the secrets-redaction MR wired
`CheckResponse`, wrote passing unit/integration tests for it, and merged — but the
policy YAML still said `tools/call: Request` from before that feature existed, so the
response-side redaction path was live code with zero traffic ever reaching it until a
live curl test against the port-forward caught the missing `redact:` log line on a
`notion_notion-get-comments` fetch. `FailClosed` only covers invoked processor
failures; a missing guardrail, or a guardrail scoped to the wrong phase, silently
fails open (or half-open) and no metric distinguishes that from working correctly.

- **Authoring (covered):** `scripts/validate.sh` renders the overlay and fails CI
  if the rendered `AgentgatewayPolicy` does not carry exactly one `tools/call ->
  mcp-cerbos-shim` guardrail with phase `Full` and `FailClosed`. A bad edit, INCLUDING
  a downgrade from `Full` back to `Request`, can't merge.
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
5. For the one tool that needs live state Cerbos can't see —
   `notion_notion-update-page` (resource `notion_page`, action `update`) — runs a
   Scratchpad **ancestry gate** BEFORE Cerbos: it calls `notion_notion-fetch` back
   through the gateway/vMCP (`internal/upstream`) once for the target page and denies
   unless the returned `<ancestor-path>` contains the Scratchpad page id from the
   `NOTION_SCRATCHPAD_PAGE_ID` env var (`deployment.yaml`, same
   `${notionScratchpadPageId}` cluster-var create-pages's Cerbos rule checks — one
   id, two independent readers). This is the only network round trip in the request
   path; it fails **closed** (deny) on lookup timeout/error/malformed result or an
   unconfigured gate. `notion_notion-fetch` must stay unmapped (`defaultAction: allow`)
   so this re-entrant call isn't itself gated — see `internal/upstream/client.go`.
6. Calls Cerbos `CheckResources` (via the `authz.Decider` interface). Denied or Cerbos error
   returns `AuthorizationError`; the deny reason is the matched rule's policy-authored `output`
   (see `policies/defs/*.yaml` `output:` blocks, e.g. `deny-self-approve`'s "use REQUEST_CHANGES
   instead") when the rule has one configured, falling back to a generic backend-agnostic
   message when it doesn't. This is what lets a calling agent understand *why* it was blocked and
   self-correct instead of retrying blind or silently downgrading its own intent.
   Allowed returns `Pass{}` — unless the tool's mapping carries a `force` block (literal key/value
   overrides, e.g. GitHub PR create/update forcing `draft: true`), in which case it returns
   `Mutated{}` with those keys rewritten into the call's arguments (re-wrapped into
   `call_tool{tool_name,parameters}` first if the call arrived that way). `force` only ever
   applies on an allowed call — a denied call is never mutated.

It never returns `header_mutation` or `metadata` (the gateway applies `metadata` even on
`Pass`, so leaving it empty is part of the contract); `mutated` is set only for a `force`-mapped
tool on allow, otherwise it's `pass` or `error`.

The shim delegates the verdict to Cerbos for almost everything; it standardizes fields
and forwards, with two narrow exceptions. A `force` block is a fixed, unconditional rewrite
(never derived from the call's own args), not a judgment call. And the Notion update-page
ancestry gate (step 5) is a shim-side deny — Cerbos can't make it, since resolving a page's
ancestors needs a network lookup and Cerbos has no I/O. Every *other* deny is Cerbos's
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
| `CONTENT_MODERATION` | unset (`""`) | `enabled`/`disabled` -- toggles the outbound content-moderation gate (see "Content Moderation" above) |
| `MODERATION_MODEL` | `omni-moderation-latest` | OpenAI Moderations model override |
| `MODERATION_WRITE_VERBS` | `create,update,save,add_note,add_comment,transition` | comma-separated verb-heuristic override |
| `PROMPT_INJECTION_DETECTION` | unset (`""`) | `enabled`/`disabled` -- toggles the response-side prompt-injection detection gate (see "Prompt Injection Detection" above) |
| `PROMPT_INJECTION_JUDGE_MODEL` | `gpt-4.1-mini` | stage-2 judge chat-completion model override |

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
