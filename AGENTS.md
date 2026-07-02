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
- **The layout is the documentation.** A user creates a new agent by copying `agents/hermes` and a
  new model/route by copying `models/sonnet`. MCP servers are not in the cluster tree — they run
  host-side under ToolHive and are declared in `host/mcp/toolhive-servers.json` (the cluster carries
  only the single `mcps/vmcp` backend/route/policy that fronts the host vMCP). Keep names
  self-explanatory and do not add explanatory comment blocks — rationale goes in the MR description,
  not inline.
- **Aggressive cleanup is expected.** Delete redundant config, comments, examples, and docs instead
  of preserving them out of habit. Keep only comments that prevent likely operational mistakes or
  explain hard-to-find gotchas; make those comments one terse line when possible. Remove values that
  merely mirror Kubernetes, controller, chart, or language defaults after verifying the default from
  the upstream source. Conversation, history, and agent/tool-specific notes belong in the MR, not the
  repo.
- **Naming** — the project, cluster, and kube context are all `vicegerent` (context `kind-vicegerent`);
  the shared agent namespace is `agent-sandbox`. Only the git repo keeps the `vicegerent-agents` name.
- **No vendor directories for cluster infra** — controller charts are pulled via Flux
  `HelmRepository`/`GitRepository`; never commit upstream chart source.
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
  auth, mTLS to the host vMCP), but it does *not* allowlist individual tools: every tool the vMCP
  exposes passes through to the agent. The only instance-level control is the `mcp-cerbos-shim` +
  Cerbos guardrail on the `vmcp` backend, which blocks one thing — reading Kubernetes **Secrets**
  (FailClosed on `tools/call`). The shim/Cerbos are NOT a tool allowlist; don't add a tool or kind
  name to the shim mapping or the Cerbos `allow` rule to *permit* something. See
  `images/mcp-cerbos-shim/README.md` ("Authorization Layers").

## Command Approval System

Hermes runs with `approvals.mode: smart` (set in `apps/base/agents/hermes/config.yaml`).
Before executing a shell command, the approval pipeline runs in this order:

1. **Hardline block** — unconditional, nothing bypasses it. Covers catastrophic commands
   (`rm -rf /`, disk formatters, fork bomb, system halt). Defined in `tools/approval.py`.
2. **Silence list** — operator-configured patterns dropped before any LLM sees them.
   Read from `apps/base/agents/hermes/approval-policy.yaml` (ConfigMap
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
