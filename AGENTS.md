# Vicegerent Agent Instructions

Vicegerent is the infra-agent platform: credential-isolated, egress-locked Hermes
agent sandboxes on a local minikube cluster, managed by Flux. The git repo is named
`vicegerent-agents`; everything inside it uses the name `vicegerent`.

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
  where possible, least-privilege RBAC, secrets via `OnePasswordItem` or Kustomize-generated
  Secrets (never hardcoded).

## Repo Conventions

- **`/apps` is user-config, `/infrastructure/controllers` is the platform.** Things a user changes
  to define an agent/model/MCP live under `apps/vicegerent` (agents, gateway, models, mcps);
  standardized controllers (agentgateway, kmcp, agent-sandbox, onepassword-connect, reloader) live
  under `infrastructure/controllers`.
- **The layout is the documentation.** A user creates a new agent by copying `agents/hermes`, a new
  model/route by copying `models/sonnet`, a new MCP by copying `mcps/kubernetes`. Keep names
  self-explanatory and do not add explanatory comment blocks — rationale goes in the MR description,
  not inline.
- **Aggressive cleanup is expected.** Delete redundant config, comments, examples, and docs instead
  of preserving them out of habit. Keep only comments that prevent likely operational mistakes or
  explain hard-to-find gotchas; make those comments one terse line when possible. Remove values that
  merely mirror Kubernetes, controller, chart, or language defaults after verifying the default from
  the upstream source. Conversation, history, and agent/tool-specific notes belong in the MR, not the
  repo.
- **Naming** — the project, cluster, and minikube profile are all `vicegerent`; the shared agent
  namespace is `agent-sandbox`. Only the git repo keeps the `vicegerent-agents` name.
- **No vendor directories** — controller charts are pulled via Flux `HelmRepository`/`GitRepository`;
  never commit upstream source.
- **Secrets model** — 1Password Connect (custom vault `Vicegerent`, which Connect can read; it cannot
  read Personal/Private/Employee vaults). Runtime app credentials and mTLS material come from
  `OnePasswordItem`s; the sandbox→agentgateway virtual API key is Kustomize-generated, not stored in
  1Password. Keep a sync job's source Secret scoped to only what it needs (e.g. a CA-only item), so
  it never gains read access to unrelated keys. Trust the published CRD behavior (e.g.
  `caCertificateRefs` resolves to a ConfigMap keyed `ca.crt`, not a Secret).
- **Generated Flux manifests** (`clusters/*/flux-system/gotk-*.yaml`) are excluded from `yamlfix`;
  leave them as Flux generates them.
- **MCP authorization layering** — agentgateway's tool-name allowlist is the single gate for
  *which* MCP tools an agent may call. The `mcp-cerbos-shim` + Cerbos policy only *block specific
  sensitive instances* (today: Kubernetes Secrets); they are NOT a second allowlist of tools/kinds.
  Don't add a tool or kind name to the shim mapping or the Cerbos `allow` rule to *permit* something
  — that belongs in the gateway allowlist. See `images/mcp-cerbos-shim/README.md` ("Division of
  responsibility").
