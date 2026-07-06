# Vicegerent Agent Instructions

Vicegerent is the infra-agent platform: credential-isolated, egress-locked Hermes
agent sandboxes on a local Kind cluster (Cilium CNI), managed by Flux. The git repo is
named `vicegerent-agents`; everything inside it uses the name `vicegerent`.

## Always

- **Don't merge** — open a merge request and let a human review and merge it.
- **Run pre-commit hooks** before every commit: `pre-commit run --files <changed files>`.
  Hooks: `check-yaml`, `trailing-whitespace`, `end-of-file-fixer`, `check-added-large-files`,
  `detect-private-key`, `yamlfix`, `detect-secrets`, and the local `validate-flux-manifests`
  (`scripts/validate.sh`).
- **Verify rendered output** before committing: `kustomize build` the changed `apps/` and
  `infrastructure/` trees, and run `scripts/validate.sh`. For a controller chart, confirm the
  `HelmRelease` and its `valuesFrom` ConfigMap render as intended.
- **Create MRs fully via the GitLab MCP tools or API** with a full, accurate description and a
  "Follow-up tasks" section for deferred work.
- **Omit Helm values that are upstream defaults** — keep `values.yaml` minimal. Include a comment
  near the top linking to the chart's upstream default values file.
- **Wait for CI to pass** before declaring an MR done.
- **Ensure every image is Renovate-compatible** — use a fully-qualified image reference with an
  explicit tag (not `latest`, not a frozen digest) so Renovate can detect and propose updates.
  `Sandbox` and `AgentgatewayParameters` images are tracked by `custom.regex` managers in
  `renovate.json`.
- **Default to secure** — non-root, no privileged containers, `automountServiceAccountToken: false`
  where possible, least-privilege RBAC, secrets applied as Kubernetes Secrets by the setup scripts
  or Kustomize-generated (never hardcoded in the repo).

## Repo Conventions

- **`/apps` is user-config, `/infrastructure/controllers` is the platform.** Things a user changes
  to define an agent/model/MCP live under `apps/base` (agents, gateway, models, mcps);
  standardized controllers (cilium, metrics-server, reloader, agentgateway, agent-sandbox,
  cerbos, mcp-cerbos-shim, host-firewall) live under `infrastructure/controllers`.
- **The layout is the documentation.** A user creates a new agent by copying
  `apps/personal/agents/hermes` within their machine's overlay, and a new model/route by copying
  `apps/base/models/anthropic`. MCP servers are not in the cluster tree — they run
  host-side under ToolHive and are declared in `host/mcp/toolhive-servers.json` (the cluster carries
  only the single `mcps/vmcp` backend/route/policy that fronts the host vMCP). Keep names
  self-explanatory and do not add explanatory comment blocks — rationale goes in the MR description,
  not inline.
- **One repo, one fork, many machines.** Each machine is its own Kind cluster with its own
  `clusters/<machine>/` (Flux entrypoint + `cluster-vars.yaml`) and `apps/<machine>/` (a thin overlay
  pulling in `../base` plus that machine's own `agents/`). `apps/base/*` stays the shared,
  machine-agnostic platform (gateway, models, mcps, searxng). The first machine is
  `personal`; stand up another by copying the `clusters/personal/` + `apps/personal/` pair, renaming
  both to the new machine, editing `clusters/<machine>/cluster-vars.yaml`, and running
  `CLUSTER_NAME=<machine> CLUSTER_PATH=./clusters/<machine> KUBE_CONTEXT=kind-<machine> ./vicegerent cluster setup && ./vicegerent bootstrap`.
- **Aggressive cleanup is expected.** Delete redundant config, comments, examples, and docs instead
  of preserving them out of habit. Default to **no comment** on config that's self-explanatory from
  its field name/value (e.g. `strategy: RollingUpdate` needs no comment saying it's a rolling update).
  Only add a comment when it prevents a likely operational mistake or explains a genuinely
  non-obvious gotcha (e.g. a CNAME chain requiring intermediate FQDNs in an allowlist) — and even
  then, one terse line, never a multi-line block. Remove values that merely mirror Kubernetes,
  controller, chart, or language defaults after verifying the default from the upstream source.
  Conversation, history, and agent/tool-specific notes belong in the MR, not the repo.
- **Naming** — the project, cluster, and kube context are all `vicegerent` (context `kind-vicegerent`);
  the shared agent namespace is `agent-sandbox`. Only the git repo keeps the `vicegerent-agents` name.
- **No vendor directories for cluster infra** — controller charts are pulled via Flux
  `HelmRepository`/`GitRepository`; never commit upstream chart source. The exception is
  `csi-driver-host-path`, whose upstream deploy pulls sidecar RBAC from five sibling repos at
  apply time (incompatible with Flux's network-isolated build), so its manifests are vendored;
  the snapshot CRDs/controller are still Flux-sourced from `external-snapshotter`.
- **Storage/backup** — the agent `data`, `gitrepos`, and `models` PVCs all use the `csi-hostpath-sc`
  StorageClass (`storage.{data,gitrepos,models}StorageClassName` in `charts/agent`) so Velero
  CSI-snapshots them.
- **Secrets model** — plain Kubernetes Secrets are the source of truth for cluster secrets (Kind
  etcd). No secret values live in git; the setup scripts (`scripts/install/setup-secrets-platform.sh`,
  `setup-secrets-agent.sh`) generate crypto material (CAs, certs, SSH keys, random tokens) and read
  user-supplied API keys from the environment or prompts, then `kubectl apply` the Secrets directly.
  MCP-server API keys are the exception — they are `thv` (ToolHive) secrets on the host, not
  Kubernetes Secrets (see `host/mcp`). Secrets are treated as disposable/recreatable — re-run the
  scripts to reseed a fresh cluster, and keep your own copy of any API keys elsewhere. The ghostunnel
  CA *key* lives only on the laptop
  under `~/.vicegerent/ghostunnel` and never enters Kubernetes (it only signs new certs). The server
  cert+key and CA cert ARE mirrored to a `ghostunnel-server` Secret so a host missing them recovers
  before ghostunnel starts (`vicegerent-mcp` `ensure_ghostunnel_material`); the cluster also gets the
  client cert (Secret) and CA cert (ConfigMap). Trust the published CRD behavior (e.g.
  `caCertificateRefs` resolves to a ConfigMap keyed `ca.crt`, not a Secret).
- **Generated Flux manifests** (`clusters/*/flux-system/gotk-*.yaml`) are excluded from `yamlfix`;
  leave them as Flux generates them.
- **MCP authorization layering** — agentgateway is the single ingress gate for MCP traffic (routing,
  auth, mTLS to the host vMCP). Two separate concerns sit on top: *tool selection* (which tools an
  agent can see/call) and *argument-level authorization*. Tool selection is done upstream in ToolHive's
  vMCP (`aggregation.tools` in `toolhive-servers.json`) so operators can scope a backend by editing
  that file and restarting the host stack; agentgateway can also enforce a per-tool allowlist itself,
  and a corporate/centralized deployment would put it there instead — which place owns it is a policy
  choice, kept in ToolHive here for developer flexibility. Argument-level authz is the `mcp-cerbos-shim`
  + Cerbos guardrail on the `vmcp` backend, a deny-by-resource guardrail (FailClosed on `tools/call`)
  that currently blocks: reading Kubernetes **Secrets**; reading the OpenSearch Grafana
  datasources; any **Jira** WRITE (create/update/transition/comment/link) targeting a project
  outside `${jiraAllowedProjects}` (by `project_key`, the project prefix of an
  issue/epic key, or an `epicKey`/`parent` reference smuggled inside
  `additional_fields`/`fields`' raw JSON — parsed by the `jiraFieldsAttr` helper,
  since that's a documented control being routed around via a side channel, not
  just an unmapped extra arg), or assigning an issue to anyone outside
  `${jiraAllowedAssignees}` (default `"*"`, i.e. unrestricted — there is no
  `reporter` field on this tool at all) — reads (`get_issue`/
  `get_project_issues`/`get_transitions`) are unrestricted, mirroring Notion's
  read-everywhere/write-scoped model; any **GitHub** call targeting a repo outside the
  allowlist in `defs/resource_github.yaml` (by `owner`/`repo`), or writing directly to a protected
  branch (main/master/production); any **Linear** `save_issue` calls that supply a team other than
  DEVOPS (`defs/resource_linear.yaml` — `save_issue` merges create+update; an ordinary update that
  omits `team` is unmapped, but an update that sets `team` is checked like a create.
  `save_comment`/`save_project` carry no verifiable team and are unmapped); any **Alertmanager**
  `createSilence` call whose duration exceeds the configured cap (`defs/resource_alertmanager.yaml`
  — `deleteSilence` has no ownership-based deny; a silence's creator can't be verified from the
  call args, so an unconditional deny would block legitimate self-cleanup exactly as much as
  it would protect anything, and was removed); any **PagerDuty** `manage_incidents` call that
  changes anything other than status=acknowledged/resolved (`defs/resource_pagerduty.yaml`); any
  **Notion** `update-page` call that sets `command: replace_content` or
  `allow_deleting_content: true` (`defs/resource_notion.yaml` — `create-pages` is unaffected, it's
  force-rewritten to the Scratchpad folder instead of denied); and any **Firecrawl**
  `firecrawl_interact` call that carries raw `code` to execute in the browser session
  (`defs/resource_firecrawl.yaml` — `prompt`-only natural-language interaction remains allowed).
  GitLab has no Cerbos-mapped tools or policy at all — its three git file/branch-write tools were
  removed from the allowlist instead (the bot has direct SSH access to gitlab.hahomelabs.com). The
  shim mapping and Cerbos rules deny protected resources; they are not the place to permit or
  block a tool outright — that's the tool-selection layer above.
  A tool's mapping can also carry a `force` block — a literal, unconditional argument rewrite applied
  only after Cerbos allows (GitHub `create_pull_request`/`update_pull_request` force `draft: true` so
  every agent-opened PR stays a draft; **Notion** `create-pages` forces `parent` to the Scratchpad
  page so every page the agent creates lands there — a rewrite, not a deny, so the agent never sees an
  error or retries). This is a mutation, not a deny; it never overrides a Cerbos denial. See
  `images/mcp-cerbos-shim/README.md` ("Authorization Layers").

## Command Approval System

Hermes runs with `approvals.mode: smart` (set in `apps/personal/agents/hermes/config.yaml`).
Before executing a shell command, the approval pipeline runs in this order:

1. **Hardline block** — unconditional, nothing bypasses it. Covers catastrophic commands
   (`rm -rf /`, disk formatters, fork bomb, system halt). Defined in `tools/approval.py`.
2. **Silence list** — operator-configured patterns dropped before any LLM sees them.
   Read from `apps/personal/agents/hermes/approval-policy.yaml` (ConfigMap
   `hermes-approval-policy`, mounted read-only at `/opt/hermes/approval-policy.yaml`).
   Tirith findings and uncancellable patterns (Hermes config escalation, credential writes,
   self-termination) are never silenced.
3. **Tirith** — static security scan. Findings that survive the silence list go here.
4. **Smart approval (haiku)** — aux LLM assesses remaining warnings. Auto-approves low-risk,
   auto-denies genuinely dangerous, escalates ambiguous to the user.

**Why not `approvals.mode: off`?** That skips steps 3 and 4 entirely — tirith never runs.
`mode: smart` + a well-tuned silence list gives zero friction on known-benign patterns while
keeping real detection on novel ones.

**Why not `TERMINAL_ENV=docker`?** The docker backend requires a Docker socket, which does
not exist in the sandbox. Setting it removes the `terminal` tool from the schema entirely.

**To add a pattern to the silence list:** edit `approval-policy.yaml`, open an MR. Reloader
watches the ConfigMap via `restart-job.yaml` and will roll the pod automatically on merge.
Do not silence tirith findings or uncancellable patterns — those are enforced in code regardless.
